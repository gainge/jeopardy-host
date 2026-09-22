"""Phase 3 acceptance gate: the transport under concurrency, hostility and crash.

The engine is stubbed here on purpose. This suite proves the pipe, not the rules:
that a broadcast reaches ten subscribers at once, that one wedged client is reaped
instead of stalling the game, that `/api/time` is honest enough to base buzz
fairness on, that the journal replays to the identical reducer input, and that a
non-host token cannot reach a host command.
"""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy import protocol as P            # noqa: E402
from jeopardy import server as S              # noqa: E402
from jeopardy.engine import Event, GameState  # noqa: E402
from jeopardy.hub import QUEUE_MAX, Hub       # noqa: E402
from jeopardy.model import Player             # noqa: E402
from jeopardy.scheduler import Scheduler, now_ms  # noqa: E402
from jeopardy.store import Store              # noqa: E402

# Valid field sets for every host-only command, so the authorization test cannot
# accidentally pass because a body failed shape validation first.
HOST_COMMAND_FIELDS = {
    P.CMD_HOST_START_ROUND: {},
    P.CMD_HOST_NEXT_ROUND: {},
    P.CMD_HOST_FORCE_RESOLVE: {},
    P.CMD_HOST_REVEAL_RESPONSE: {},
    P.CMD_HOST_UNDO: {},
    P.CMD_HOST_ADJUST_SCORE: {"player_id": "p_x", "delta": -400, "reason": "misjudged"},
    P.CMD_HOST_SET_CONTROL: {"player_id": "p_x"},
    P.CMD_HOST_KICK: {"player_id": "p_x"},
}


class FakeEngine:
    """Records its input and emits one echo event per command.

    Phase 2 owns the real rules; all this needs to be is deterministic, so that
    `log` is a faithful fingerprint of what the journal fed the reducer.
    """

    def __init__(self, game=None):
        self.game = game
        self.state = GameState(phase=P.PHASE_LOBBY)
        self.log = []
        self.deadline = None

    def apply(self, actor_id, ctype, fields, now_ms):
        self.log.append((actor_id, ctype, dict(fields), now_ms))
        if ctype == P.CMD_JOIN:
            # Mirrors the real engine: ids are minted here, deterministically,
            # and the first arrival is the host.
            self.state.joined += 1
            pid = f"p{self.state.joined}"
            self.state.players[pid] = Player(id=pid, name=fields["name"],
                                             is_host=not self.state.order)
            self.state.order.append(pid)
            return [Event(P.EVT_PLAYERS,
                          {"players": [self.state.players[p].to_dict()
                                       for p in self.state.order],
                           "player_id": pid})]
        return [Event("echo", {"ctype": ctype, "fields": fields,
                               "actor": actor_id})]

    def tick(self, now_ms):
        self.log.append((None, "__tick__", {}, now_ms))
        self.deadline = None
        return [Event("ticked", {"now": now_ms})]

    def next_deadline(self):
        return self.deadline

    def snapshot_for(self, player_id):
        return {"phase": self.state.phase, "you": player_id,
                "is_host": bool(self.state.players[player_id].is_host)
                if player_id in self.state.players else False}


def make_app(tmpdir, admin_token="ADMIN", engine=None):
    engine = engine or FakeEngine()
    store = Store(os.path.join(tmpdir, "events.jsonl"))
    app = S.GameServer(engine, store,
                       static_dir=os.path.join(tmpdir, "static"),
                       media_dir=os.path.join(tmpdir, "media"),
                       admin_token=admin_token)
    return app


class HubTest(unittest.TestCase):
    def test_ten_concurrent_subscribers_all_receive_a_broadcast(self):
        hub = Hub()
        subs = [hub.subscribe(f"p{i}") for i in range(10)]
        received = {}
        ready = threading.Barrier(11)

        def drain(sub):
            ready.wait()
            frames = []
            for frame in sub.stream():
                if frame.startswith(b"event:"):
                    frames.append(frame)
                if len(frames) == 3:
                    break
            received[sub.player_id] = frames

        threads = [threading.Thread(target=drain, args=(s,), daemon=True)
                   for s in subs]
        for t in threads:
            t.start()
        ready.wait()
        for n in range(3):
            hub.broadcast(P.EVT_BUZZERS_OPEN, {"clue_id": f"J_1_{n}"})
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(received), 10)
        for pid, frames in received.items():
            self.assertEqual(len(frames), 3, pid)
            first = frames[0].decode()
            self.assertTrue(first.startswith(f"event: {P.EVT_BUZZERS_OPEN}\n"))
            self.assertEqual(json.loads(first.split("data: ", 1)[1].strip()),
                             {"clue_id": "J_1_0"})

    def test_a_wedged_subscriber_is_reaped_without_blocking_the_others(self):
        reaped = []
        hub = Hub(on_reap=reaped.append)
        wedged = hub.subscribe("stuck")
        live = hub.subscribe("live")

        total = QUEUE_MAX * 3
        drained = []
        done = threading.Event()

        def consume():
            for frame in live.stream():
                if frame.startswith(b"event:"):
                    drained.append(frame)
                if len(drained) == total:
                    done.set()
                    return

        threading.Thread(target=consume, daemon=True).start()

        slowest = 0.0
        for n in range(total):
            started = time.monotonic()
            hub.broadcast(P.EVT_BUZZ_REGISTERED, {"clue_id": "J_1_1", "n": n})
            slowest = max(slowest, time.monotonic() - started)
            # A real game emits events seconds apart; let the healthy consumer
            # keep pace so only the genuinely wedged client overflows.
            waited = time.monotonic() + 5
            while n - len(drained) > QUEUE_MAX // 2 and time.monotonic() < waited:
                time.sleep(0.001)

        self.assertLess(slowest, 0.05, "broadcast blocked on the wedged client")
        self.assertFalse(wedged.alive)
        self.assertTrue(wedged.dropped)
        self.assertEqual(reaped, ["stuck"])
        self.assertTrue(live.alive)
        self.assertEqual(hub.connected_ids(), ["live"])
        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(len(drained), total)

    def test_idle_stream_emits_a_heartbeat_comment(self):
        hub = Hub()
        sub = hub.subscribe("p1")
        import jeopardy.hub as hub_mod
        original = hub_mod.HEARTBEAT_S
        hub_mod.HEARTBEAT_S = 0.05
        try:
            self.assertEqual(next(iter(sub.stream())), b": heartbeat\n\n")
        finally:
            hub_mod.HEARTBEAT_S = original

    def test_unsubscribe_ends_the_stream(self):
        hub = Hub()
        sub = hub.subscribe("p1")
        hub.broadcast(P.EVT_PLAYERS, {"players": []})
        frames = iter(sub.stream())
        self.assertTrue(next(frames).startswith(b"event: players"))
        hub.unsubscribe(sub)
        with self.assertRaises(StopIteration):
            next(frames)


class SchedulerTest(unittest.TestCase):
    def test_fires_at_the_deadline_and_can_be_pulled_earlier(self):
        fired = []
        sched = Scheduler(fired.append)
        sched.start()
        self.addCleanup(sched.stop)

        sched.schedule(now_ms() + 3000)
        sched.schedule(now_ms() + 60)     # earlier wake must win
        deadline = time.monotonic() + 2
        while not fired and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(fired), 1)
        self.assertLessEqual(abs(fired[0] - now_ms()), 250)

    def test_a_later_request_does_not_postpone_a_pending_wake(self):
        fired = []
        sched = Scheduler(fired.append)
        sched.start()
        self.addCleanup(sched.stop)
        sched.schedule(now_ms() + 60)
        sched.schedule(now_ms() + 5000)
        time.sleep(0.4)
        self.assertEqual(len(fired), 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def replay_into(self, path, engine):
        store = Store(path)
        try:
            store.replay(engine)
        finally:
            store.close()

    def test_journal_replays_to_identical_reducer_input_after_a_crash(self):
        path = os.path.join(self.tmp.name, "events.jsonl")
        engine = FakeEngine()
        store = Store(path)
        store.append("p_a", P.CMD_SELECT_CLUE, {"clue_id": "J_1_1"}, 1000)
        engine.apply("p_a", P.CMD_SELECT_CLUE, {"clue_id": "J_1_1"}, 1000)
        store.append("p_b", P.CMD_BUZZ,
                     {"clue_id": "J_1_1", "press_server": 1500,
                      "offset_ms": -3, "rtt_ms": 20}, 1520)
        engine.apply("p_b", P.CMD_BUZZ,
                     {"clue_id": "J_1_1", "press_server": 1500,
                      "offset_ms": -3, "rtt_ms": 20}, 1520)
        store.append(None, "__tick__", {}, 1800)
        engine.tick(1800)
        store.close()   # the crash

        revived = FakeEngine()
        self.replay_into(path, revived)
        self.assertEqual(revived.log, engine.log)

    def test_replay_skips_transport_bookkeeping_entries(self):
        path = os.path.join(self.tmp.name, "events.jsonl")
        store = Store(path)
        store.append("p_a", "__token__", {"player_id": "p_a", "token": "t"}, 10)
        store.append("p_a", P.CMD_SELECT_CLUE, {"clue_id": "J_1_1"}, 20)
        store.close()
        engine = FakeEngine()
        self.replay_into(path, engine)
        self.assertEqual([e[1] for e in engine.log], [P.CMD_SELECT_CLUE])

    def test_a_truncated_final_line_is_tolerated(self):
        path = os.path.join(self.tmp.name, "events.jsonl")
        store = Store(path)
        store.append("p_a", P.CMD_SELECT_CLUE, {"clue_id": "J_1_1"}, 20)
        store.close()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"actor": "p_b", "type": "bu')
        engine = FakeEngine()
        self.replay_into(path, engine)
        self.assertEqual(len(engine.log), 1)


class ServerReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_restart_restores_seats_tokens_and_host(self):
        app = make_app(self.tmp.name)
        host = app.join("Gibson", admin_token="ADMIN")
        guest = app.join("Ada")
        self.assertTrue(host["is_host"])
        app.command(guest["token"], {"type": P.CMD_SELECT_CLUE,
                                     "clue_id": "J_2_3"})
        app.store.close()

        revived = make_app(self.tmp.name)
        self.addCleanup(revived.stop)
        revived.resume()

        self.assertEqual(revived.engine.log, app.engine.log)
        self.assertEqual(revived.host_id, host["player_id"])
        self.assertEqual(revived.resolve(host["token"]), host["player_id"])
        self.assertEqual(revived.resolve(guest["token"]), guest["player_id"])


class HttpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "static"))
        os.makedirs(os.path.join(self.tmp.name, "media"))
        with open(os.path.join(self.tmp.name, "static", "index.html"), "w") as fh:
            fh.write("<h1>board</h1>")
        with open(os.path.join(self.tmp.name, "static", "app.js"), "w") as fh:
            fh.write("export const ok = 1;\n")
        with open(os.path.join(self.tmp.name, "secret.txt"), "w") as fh:
            fh.write("outside the served roots")

        self.app = make_app(self.tmp.name)
        self.port = self.app.bind("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.app.stop)

    def conn(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        return c

    def post(self, path, payload, token=None, conn=None, raw=None, headers=None):
        c = conn or self.conn()
        body = raw if raw is not None else json.dumps(payload).encode()
        hdrs = {"Content-Type": "application/json"}
        if token:
            hdrs[S.TOKEN_HEADER] = token
        hdrs.update(headers or {})
        c.request("POST", path, body=body, headers=hdrs)
        resp = c.getresponse()
        data = resp.read()
        return resp.status, data

    def join(self, name, admin_token=None):
        payload = {"name": name}
        if admin_token:
            payload["admin_token"] = admin_token
        status, data = self.post("/api/join", payload)
        self.assertEqual(status, 200, data)
        return json.loads(data)

    def seat_host(self):
        return self.join("Gibson", admin_token="ADMIN")


class TimeEndpointTest(HttpTestCase):
    def test_skew_is_small_and_monotonic(self):
        c = self.conn()
        samples = []
        for _ in range(25):
            before = now_ms()
            c.request("GET", "/api/time")
            resp = c.getresponse()
            payload = json.loads(resp.read())
            after = now_ms()
            self.assertEqual(resp.status, 200)
            samples.append((before, payload["server_ms"], after))

        for before, server_ms, after in samples:
            # Same machine, same clock: the reading must land inside the very
            # window in which it was taken, or offset estimation is meaningless.
            self.assertGreaterEqual(server_ms, before - 2)
            self.assertLessEqual(server_ms, after + 2)

        readings = [s[1] for s in samples]
        self.assertEqual(readings, sorted(readings))

        skews = [abs(s[1] - (s[0] + s[2]) // 2) for s in samples]
        self.assertLess(sorted(skews)[len(skews) // 2], 50)

    def test_time_needs_no_token_and_keeps_the_connection_alive(self):
        c = self.conn()
        for _ in range(3):
            c.request("GET", "/api/time")
            resp = c.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("server_ms", json.loads(resp.read()))


class AuthorizationTest(HttpTestCase):
    def test_every_host_command_is_refused_for_a_non_host_token(self):
        host = self.seat_host()
        guest = self.join("Ada")
        self.assertTrue(host["is_host"])
        self.assertFalse(guest["is_host"])

        for ctype, fields in HOST_COMMAND_FIELDS.items():
            payload = {"type": ctype, **fields}
            status, data = self.post("/api/command", payload, token=guest["token"])
            self.assertEqual(status, 403, f"{ctype}: {data!r}")
            status, data = self.post("/api/command", payload, token=host["token"])
            self.assertEqual(status, 202, f"{ctype}: {data!r}")

        applied = [e[1] for e in self.app.engine.log]
        for ctype in HOST_COMMAND_FIELDS:
            self.assertEqual(applied.count(ctype), 1, ctype)

    def test_host_commands_cover_the_whole_protocol_set(self):
        self.assertEqual(set(HOST_COMMAND_FIELDS), set(P.HOST_COMMANDS))

    def test_the_first_seat_is_refused_without_the_admin_token(self):
        status, data = self.post("/api/join", {"name": "Impostor"})
        self.assertEqual(status, 403, data)
        self.assertEqual(self.app.engine.log, [])

        first = self.join("Gibson", admin_token="ADMIN")
        self.assertTrue(first["is_host"])
        # Once the host is seated the door is open, and the token buys nothing more.
        second = self.join("Ada", admin_token="ADMIN")
        self.assertFalse(second["is_host"])

    def test_a_wrong_or_missing_token_is_rejected(self):
        self.seat_host()
        status, _ = self.post("/api/command",
                              {"type": P.CMD_HOST_UNDO}, token="nonsense")
        self.assertEqual(status, 401)
        status, _ = self.post("/api/command", {"type": P.CMD_HOST_UNDO})
        self.assertEqual(status, 401)

    def test_join_is_not_reachable_as_a_command(self):
        self.seat_host()
        player = self.join("Ada")
        status, _ = self.post("/api/command",
                              {"type": P.CMD_JOIN, "name": "Ada2"},
                              token=player["token"])
        self.assertEqual(status, 400)


class BadRequestTest(HttpTestCase):
    def test_malformed_bodies_are_rejected(self):
        self.seat_host()
        player = self.join("Ada")
        for raw in (b"not json", b"[]", b"{}", b'{"type":"nope"}',
                    b'{"type":"select_clue"}',
                    b'{"type":"select_clue","clue_id":42}',
                    b'{"type":"buzz","clue_id":"J_1_1"}'):
            status, data = self.post("/api/command", None, token=player["token"],
                                     raw=raw)
            self.assertEqual(status, 400, f"{raw!r} -> {data!r}")
        self.assertEqual([e[1] for e in self.app.engine.log],
                         [P.CMD_JOIN, P.CMD_JOIN])

    def test_an_oversized_body_is_rejected_without_being_read(self):
        player = self.seat_host()
        blob = json.dumps({"type": P.CMD_FINAL_ANSWER,
                           "text": "x" * (S.MAX_BODY * 2)}).encode()
        status, _ = self.post("/api/command", None, token=player["token"], raw=blob)
        self.assertEqual(status, 413)

    def test_a_lying_content_length_is_rejected(self):
        player = self.seat_host()
        status, _ = self.post("/api/command", None, token=player["token"],
                              raw=b"{}", headers={"Content-Length": str(S.MAX_BODY + 1)})
        self.assertEqual(status, 413)

    def test_extra_fields_are_dropped_rather_than_smuggled(self):
        player = self.seat_host()
        status, _ = self.post("/api/command",
                              {"type": P.CMD_SELECT_CLUE, "clue_id": "J_1_1",
                               "is_daily_double": True, "answer": "who is x"},
                              token=player["token"])
        self.assertEqual(status, 202)
        _, _, fields, _ = self.app.engine.log[-1]
        self.assertEqual(fields, {"clue_id": "J_1_1"})


class StaticFileTest(HttpTestCase):
    def get(self, path):
        c = self.conn()
        c.request("GET", path)
        resp = c.getresponse()
        return resp.status, resp.getheader("Content-Type"), resp.read()

    def test_index_and_modules_are_served_with_usable_types(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"board", body)
        status, ctype, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/javascript")

    def test_path_traversal_is_impossible(self):
        for attack in ("/static/../secret.txt",
                       "/static/%2e%2e/secret.txt",
                       "/static/..%2fsecret.txt",
                       "/media/../../etc/passwd",
                       "/static/....//secret.txt"):
            status, _, body = self.get(attack)
            self.assertEqual(status, 404, f"{attack} -> {body[:80]!r}")

    def test_safe_path_rejects_escapes_directly(self):
        root = os.path.join(self.tmp.name, "static")
        self.assertIsNone(S.safe_path(root, "../secret.txt"))
        self.assertIsNone(S.safe_path(root, "..%2f..%2fsecret.txt"))
        # A leading slash is re-rooted rather than honoured as absolute.
        self.assertEqual(S.safe_path(root, "/etc/passwd"),
                         os.path.join(os.path.realpath(root), "etc/passwd"))
        self.assertIsNotNone(S.safe_path(root, "app.js"))


class SseStreamTest(HttpTestCase):
    def open_stream(self, token):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        fh = sock.makefile("rb")
        # makefile keeps the descriptor alive, so both halves must be closed for
        # the server to see a real disconnect.
        self.addCleanup(sock.close)
        self.addCleanup(fh.close)
        sock.sendall(f"GET /events?token={token} HTTP/1.1\r\n"
                     f"Host: 127.0.0.1\r\n\r\n".encode())
        self.assertIn(b"200", fh.readline())
        while fh.readline().strip():
            pass
        return sock, fh

    def read_until(self, fh, wanted, limit=20):
        for _ in range(limit):
            etype, data = self.read_frame(fh)
            if etype == wanted:
                return data
        raise AssertionError(f"no {wanted} frame arrived")

    def read_frame(self, fh):
        lines = []
        while True:
            line = fh.readline()
            if not line:
                raise AssertionError("stream closed")
            if line in (b"\n", b"\r\n"):
                break
            lines.append(line.decode().rstrip("\r\n"))
            if lines[0].startswith(":"):
                return lines[0], None
        etype = lines[0].split("event: ", 1)[1]
        data = json.loads(lines[1].split("data: ", 1)[1])
        return etype, data

    def test_a_stream_opens_with_a_snapshot_then_receives_broadcasts(self):
        player = self.seat_host()
        _, fh = self.open_stream(player["token"])

        etype, data = self.read_frame(fh)
        self.assertEqual(etype, P.EVT_SNAPSHOT)
        self.assertEqual(data["you"], player["player_id"])
        etype, _ = self.read_frame(fh)
        self.assertEqual(etype, P.EVT_PLAYERS)

        self.post("/api/command",
                  {"type": P.CMD_SELECT_CLUE, "clue_id": "J_3_2"},
                  token=player["token"])
        etype, data = self.read_frame(fh)
        self.assertEqual(etype, "echo")
        self.assertEqual(data["fields"], {"clue_id": "J_3_2"})

    def test_two_streams_both_see_the_same_broadcast(self):
        a = self.seat_host()
        b = self.join("Grace")
        _, fa = self.open_stream(a["token"])
        _, fb = self.open_stream(b["token"])
        deadline = time.monotonic() + 5
        while len(self.app.hub.connected_ids()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.app.hub.connected_ids()), 2)

        self.post("/api/command", {"type": P.CMD_BUZZ, "clue_id": "J_1_1",
                                   "press_server": now_ms(), "offset_ms": 0,
                                   "rtt_ms": 12}, token=a["token"])
        for fh in (fa, fb):
            data = self.read_until(fh, "echo")
            self.assertEqual(data["ctype"], P.CMD_BUZZ)

    def test_a_stream_without_a_token_is_refused(self):
        c = self.conn()
        c.request("GET", "/events")
        resp = c.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401)

    def test_a_dropped_stream_marks_the_player_disconnected(self):
        player = self.seat_host()
        sock, fh = self.open_stream(player["token"])
        self.read_frame(fh)
        pid = player["player_id"]
        self.assertTrue(self.app.engine.state.players[pid].connected)

        fh.close()
        sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.app.engine.state.players[pid].connected:
                break
            self.app.hub.broadcast(P.EVT_PLAYERS, {"players": []})
            time.sleep(0.05)
        self.assertFalse(self.app.engine.state.players[pid].connected)


class TickTest(HttpTestCase):
    def test_an_engine_deadline_is_journaled_and_broadcast(self):
        player = self.seat_host()
        _, fh = SseStreamTest.open_stream(self, player["token"])
        self.app.engine.deadline = now_ms() + 80
        self.app.scheduler.schedule(self.app.engine.deadline)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e[1] == "__tick__" for e in self.app.engine.log):
                break
            time.sleep(0.02)
        self.assertTrue(any(e[1] == "__tick__" for e in self.app.engine.log))
        self.assertTrue(any(e["type"] == "__tick__"
                            for e in self.app.store.entries()))


if __name__ == "__main__":
    unittest.main()
