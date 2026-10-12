"""Phase 5: the whole system, over real HTTP, on a real socket.

Every other suite tests one layer against fakes. This one boots the actual server,
opens actual SSE streams, and drives a game through actual POSTs — because the bugs
that matter here live in the seams between the four phases.

The central claim under test: a player on a slow link who pressed first beats a
player on a fast link who pressed second. Each simulated client carries its own
clock skew and its own injected latency, so a regression that reintroduces
first-packet-wins fails here and nowhere else.
"""

import json
import os
import queue
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy import protocol as P  # noqa: E402
from jeopardy import server as S  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "fixtures", "expected_9537.json")

with open(GOLDEN, encoding="utf-8") as _fh:
    GAME_DICT = json.load(_fh)
ALL_ANSWERS = [c["answer"] for r in GAME_DICT["rounds"].values()
               for c in r["clues"].values() if len(c["answer"]) > 3]


class Client:
    """One simulated player: its own clock skew, its own link latency, its own stream."""

    def __init__(self, base, name, admin_token=None, skew_ms=0, latency_ms=0):
        self.base = base
        self.name = name
        self.skew_ms = skew_ms          # this machine's clock is wrong by this much
        self.latency_ms = latency_ms    # one-way delay added to outbound requests
        self.events = queue.Queue()
        self.seen = []
        body = {"name": name}
        if admin_token:
            body["admin_token"] = admin_token
        seat = self._post("/api/join", body)
        self.player_id = seat["player_id"]
        self.token = seat["token"]
        self.is_host = seat.get("is_host", False)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    # -- plumbing

    def _post(self, path, body):
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw else {}

    def _listen(self):
        url = f"{self.base}/events?token={self.token}"
        with urllib.request.urlopen(url, timeout=30) as stream:
            etype = None
            for raw in stream:
                if self._stop.is_set():
                    return
                line = raw.decode().rstrip("\n")
                if line.startswith("event:"):
                    etype = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and etype:
                    payload = json.loads(line.split(":", 1)[1].strip())
                    self.seen.append((etype, payload))
                    self.events.put((etype, payload))
                    etype = None

    def send(self, ctype, **fields):
        return self._post(f"/api/command?token={self.token}",
                          dict(type=ctype, **fields))

    def wait_for(self, etype, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                got, payload = self.events.get(timeout=deadline - time.time())
            except queue.Empty:
                break
            if got == etype:
                return payload
        raise AssertionError(f"{self.name}: never saw {etype}; "
                             f"saw {[e for e, _ in self.seen]}")

    def drain(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    # -- the part under test

    def buzz(self, clue_id, press_server_ms):
        """Press at a stated server-time instant, then pay this link's latency."""
        return self.send(P.CMD_BUZZ, clue_id=clue_id,
                         press_server=int(press_server_ms),
                         offset_ms=-self.skew_ms, rtt_ms=self.latency_ms * 2)

    def close(self):
        self._stop.set()


class EndToEndTest(unittest.TestCase):
    """Each test gets its own server and its own game, because a shared board would
    let one test's spent clues and seated host leak into the next."""

    def setUp(self):
        import shutil
        import tempfile
        from jeopardy.engine import Engine
        from jeopardy.model import Game
        from jeopardy.store import Store

        self.tmp = tempfile.mkdtemp(prefix="jeopardy-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = S.GameServer(
            Engine(Game.from_dict(GAME_DICT)),
            Store(os.path.join(self.tmp, "events.jsonl")),
            static_dir=os.path.join(os.path.dirname(HERE), "static"),
            media_dir=os.path.join(self.tmp, "media"),
            admin_token="test-admin-token",
        )
        self.port = self.app.bind("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.clients = []
        self.addCleanup(self.app.stop)
        self.addCleanup(lambda: [c.close() for c in self.clients])

    def join(self, name, **kw):
        c = Client(self.base, name, **kw)
        self.clients.append(c)
        return c

    def server_now(self):
        with urllib.request.urlopen(self.base + "/api/time", timeout=5) as r:
            return json.load(r)["server_ms"]

    # -- tests

    def test_time_endpoint_is_usable_for_clock_sync(self):
        samples = []
        for _ in range(7):
            t0 = time.time() * 1000
            server_ms = self.server_now()
            t1 = time.time() * 1000
            samples.append((t1 - t0, server_ms - (t0 + (t1 - t0) / 2)))
        rtt, offset = min(samples)
        self.assertLess(rtt, 100, "local round trip should be fast")
        self.assertLess(abs(offset), 50, f"offset against our own clock was {offset}ms")

    def test_full_clue_cycle_over_http(self):
        host = self.join("Gibson", admin_token="test-admin-token")
        alice = self.join("Alice")
        self.assertTrue(host.is_host)
        self.assertFalse(alice.is_host)

        host.wait_for(P.EVT_SNAPSHOT)
        alice.wait_for(P.EVT_SNAPSHOT)
        host.send(P.CMD_HOST_START_ROUND)
        board = alice.wait_for(P.EVT_ROUND_STARTED)
        self.assertEqual(len(board["categories"]), 6)

        host.send(P.CMD_SELECT_CLUE, clue_id="J_2_1")
        selected = alice.wait_for(P.EVT_CLUE_SELECTED)
        self.assertNotIn("archipelago", json.dumps(selected),
                         "the answer leaked at selection time")
        self.assertNotIn("is_daily_double", json.dumps(selected))

        alice.wait_for(P.EVT_BUZZERS_OPEN, timeout=12)
        alice.buzz("J_2_1", self.server_now())
        alice.wait_for(P.EVT_BUZZ_RESULT, timeout=6)
        answering = alice.wait_for(P.EVT_ANSWERING)
        self.assertEqual(answering["player_id"], alice.player_id)

        alice.send(P.CMD_SELF_REPORT, clue_id="J_2_1", correct=True)
        resolved = alice.wait_for(P.EVT_CLUE_RESOLVED, timeout=6)
        self.assertEqual(resolved["answer"], "archipelago")
        self.assertEqual(resolved["scores"][alice.player_id], 200)

        # The answer screen holds the board until the host returns everyone to it.
        alice.send(P.CMD_SELECT_CLUE, clue_id="J_2_2")
        self.assertIn("host", alice.wait_for(P.EVT_ERROR)["message"])
        host.send(P.CMD_HOST_RETURN_TO_BOARD)
        self.assertEqual(alice.wait_for(P.EVT_CLUE_DISMISSED), {"clue_id": "J_2_1"})
        self.assertEqual(host.wait_for(P.EVT_CLUE_DISMISSED), {"clue_id": "J_2_1"})
        alice.send(P.CMD_SELECT_CLUE, clue_id="J_2_2")
        self.assertEqual(alice.wait_for(P.EVT_CLUE_SELECTED)["clue_id"], "J_2_2")

    def test_slow_link_that_pressed_first_beats_fast_link_that_pressed_second(self):
        """The claim the whole design exists to support."""
        host = self.join("Gibson", admin_token="test-admin-token")
        # Alice is far away and her clock runs 250ms fast; Bob is local.
        alice = self.join("Alice", skew_ms=250, latency_ms=180)
        bob = self.join("Bob", skew_ms=-40, latency_ms=5)
        for c in (host, alice, bob):
            c.wait_for(P.EVT_SNAPSHOT)

        host.send(P.CMD_HOST_START_ROUND)
        for c in (host, alice, bob):
            c.wait_for(P.EVT_ROUND_STARTED)
        host.send(P.CMD_SELECT_CLUE, clue_id="J_3_1")
        for c in (host, alice, bob):
            c.wait_for(P.EVT_CLUE_SELECTED)
        opened = bob.wait_for(P.EVT_BUZZERS_OPEN, timeout=12)
        self.assertEqual(opened["clue_id"], "J_3_1")

        unlock = self.server_now()
        # Alice presses 30ms after unlock. Bob presses 90ms after unlock — but his
        # packet arrives long before hers, because her link is 175ms slower.
        threads = [
            threading.Thread(target=alice.buzz, args=("J_3_1", unlock + 30)),
            threading.Thread(target=bob.buzz, args=("J_3_1", unlock + 90)),
        ]
        threads[1].start()          # Bob's request is even dispatched first
        threads[0].start()
        for t in threads:
            t.join()

        result = bob.wait_for(P.EVT_BUZZ_RESULT, timeout=6)
        order = [e["player_id"] for e in result["order"]]
        self.assertEqual(order[0], alice.player_id,
                         "the earlier press must win regardless of arrival order")
        self.assertEqual(order[1], bob.player_id)
        self.assertGreater(result["order"][1]["delta_ms"], 0)

        answering = bob.wait_for(P.EVT_ANSWERING)
        self.assertEqual(answering["player_id"], alice.player_id)

    def test_wrong_answer_passes_to_the_next_fastest_buzzer(self):
        host = self.join("Gibson", admin_token="test-admin-token")
        alice = self.join("Alice")
        bob = self.join("Bob")
        for c in (host, alice, bob):
            c.wait_for(P.EVT_SNAPSHOT)
        host.send(P.CMD_HOST_START_ROUND)
        host.send(P.CMD_SELECT_CLUE, clue_id="J_4_1")
        for c in (alice, bob):
            c.wait_for(P.EVT_CLUE_SELECTED)
        alice.wait_for(P.EVT_BUZZERS_OPEN, timeout=12)

        unlock = self.server_now()
        alice.buzz("J_4_1", unlock + 20)
        bob.buzz("J_4_1", unlock + 60)
        alice.wait_for(P.EVT_BUZZ_RESULT, timeout=6)
        self.assertEqual(alice.wait_for(P.EVT_ANSWERING)["player_id"], alice.player_id)

        alice.send(P.CMD_SELF_REPORT, clue_id="J_4_1", correct=False)
        result = bob.wait_for(P.EVT_ANSWER_RESULT, timeout=6)
        self.assertFalse(result["correct"])
        self.assertLess(result["score_delta"], 0)
        self.assertEqual(bob.wait_for(P.EVT_ANSWERING, timeout=6)["player_id"],
                         bob.player_id)

    def test_early_press_is_ignored_and_may_press_again(self):
        host = self.join("Gibson", admin_token="test-admin-token")
        alice = self.join("Alice")
        for c in (host, alice):
            c.wait_for(P.EVT_SNAPSHOT)
        host.send(P.CMD_HOST_START_ROUND)
        host.send(P.CMD_SELECT_CLUE, clue_id="J_5_1")
        selected = alice.wait_for(P.EVT_CLUE_SELECTED)

        alice.buzz("J_5_1", selected["unlock_at"] - 500)   # jumped the gun
        alice.wait_for(P.EVT_BUZZERS_OPEN, timeout=12)
        alice.drain()
        alice.buzz("J_5_1", self.server_now() + 5)          # and again, legally
        result = alice.wait_for(P.EVT_BUZZ_RESULT, timeout=6)
        self.assertEqual([e["player_id"] for e in result["order"]], [alice.player_id],
                         "an early press must not forfeit the clue")

    def test_no_answer_reaches_any_client_before_its_clue_resolves(self):
        """Walks every frame every client received and looks for a spoiler."""
        host = self.join("Gibson", admin_token="test-admin-token")
        alice = self.join("Alice")
        for c in (host, alice):
            c.wait_for(P.EVT_SNAPSHOT)
        host.send(P.CMD_HOST_START_ROUND)
        host.send(P.CMD_SELECT_CLUE, clue_id="J_6_1")
        alice.wait_for(P.EVT_CLUE_SELECTED)
        alice.wait_for(P.EVT_BUZZERS_OPEN, timeout=12)
        alice.buzz("J_6_1", self.server_now())
        alice.wait_for(P.EVT_BUZZ_RESULT, timeout=6)
        alice.wait_for(P.EVT_ANSWERING)
        alice.send(P.CMD_SELF_REPORT, clue_id="J_6_1", correct=True)
        alice.wait_for(P.EVT_CLUE_RESOLVED, timeout=6)

        for client in (host, alice):
            for etype, payload in client.seen:
                if etype in (P.EVT_CLUE_RESOLVED, P.EVT_FINAL_REVEAL):
                    continue
                blob = json.dumps(payload, ensure_ascii=False)
                leaked = [a for a in ALL_ANSWERS if a in blob]
                self.assertFalse(
                    leaked,
                    f"{client.name} received {etype} carrying answer(s) {leaked}")
                self.assertNotIn("is_daily_double", blob,
                                 f"{client.name} received a DD marker in {etype}")

    def test_host_commands_are_refused_for_a_plain_player(self):
        host = self.join("Gibson", admin_token="test-admin-token")
        alice = self.join("Alice")
        for c in (host, alice):
            c.wait_for(P.EVT_SNAPSHOT)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            alice.send(P.CMD_HOST_ADJUST_SCORE,
                       player_id=alice.player_id, delta=5000, reason="nice try")
        self.assertEqual(caught.exception.code, 403)

    def test_a_claimed_host_seat_needs_the_admin_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            Client(self.base, "Impostor", admin_token="wrong-token")
        self.assertEqual(caught.exception.code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
