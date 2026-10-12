"""Phase 2 behavioral spec: a full scripted game, plus the buzzer race rules.

The load-bearing assertion in this file is `test_later_arrival_earlier_press_wins`.
Everything else in the buzzer design exists to serve it: a player on a slow link who
pressed first must beat a player on a fast link who pressed second.
"""

import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy import buzzer  # noqa: E402
from jeopardy import protocol as P  # noqa: E402
from jeopardy.engine import Engine, GameState  # noqa: E402
from jeopardy.model import (BUZZ_ACCEPTED, BUZZ_CLAMPED, BUZZ_DUPLICATE,  # noqa: E402
                            BUZZ_EARLY, BUZZ_FUTURE, Buzz, Game)

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "fixtures", "expected_9537.json")

with open(GOLDEN, encoding="utf-8") as _fh:
    GAME_DICT = json.load(_fh)

J_DD = "J_1_4"          # the Daily Double in the fixture's Jeopardy round
DJ_DD = ("DJ_1_3", "DJ_2_3")


def types(events):
    return [e.type for e in events]


def first(events, etype):
    for e in events:
        if e.type == etype:
            return e
    raise AssertionError(f"no {etype} in {types(events)}")


class EngineFixture(unittest.TestCase):
    """Three players, the host among them, seated and ready to start."""

    def setUp(self):
        self.game = Game.from_dict(GAME_DICT)
        self.engine = Engine(self.game)
        self.now = 1_000_000
        self.ids = {}
        for name in ("host", "alice", "bob"):
            evts = self.engine.apply(None, P.CMD_JOIN, {"name": name}, self.now)
            self.ids[name] = evts[0].payload["player_id"]
        self.host = self.ids["host"]
        self.alice = self.ids["alice"]
        self.bob = self.ids["bob"]

    def do(self, actor, ctype, fields=None):
        return self.engine.apply(actor, ctype, fields or {}, self.now)

    def tick(self, advance=0):
        self.now += advance
        return self.engine.tick(self.now)

    def score(self, pid):
        return self.engine.state.players[pid].score

    def open_clue(self, selector, clue_id):
        """Select a plain clue and run the reading countdown out."""
        events = self.do(selector, P.CMD_SELECT_CLUE, {"clue_id": clue_id})
        unlock = first(events, P.EVT_CLUE_SELECTED).payload["unlock_at"]
        self.now = unlock
        opened = self.engine.tick(self.now)
        first(opened, P.EVT_BUZZERS_OPEN)
        return unlock

    def buzz(self, pid, clue_id, press, at, rtt=0):
        """Deliver a buzz that was pressed at `press` and landed at `at`."""
        self.now = at
        return self.engine.apply(pid, P.CMD_BUZZ, {
            "clue_id": clue_id, "press_server": press,
            "offset_ms": 0, "rtt_ms": rtt}, at)

    def resolve_pending(self):
        """Drain the scheduled resolution and return the clue_resolved event."""
        return first(self.engine.tick(self.now), P.EVT_CLUE_RESOLVED)

    def return_to_board(self):
        return first(self.do(self.host, P.CMD_HOST_RETURN_TO_BOARD),
                     P.EVT_CLUE_DISMISSED)


class PurityTest(unittest.TestCase):
    """Restated here so the engine's own suite fails loudly if purity regresses."""

    FORBIDDEN = {"time", "socket", "http", "threading", "random", "urllib",
                 "datetime", "os", "secrets", "uuid"}

    def test_engine_imports_nothing_impure(self):
        path = os.path.join(os.path.dirname(HERE), "jeopardy", "engine.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & self.FORBIDDEN)

    def test_player_ids_are_deterministic(self):
        game = Game.from_dict(GAME_DICT)
        runs = []
        for _ in range(2):
            eng = Engine(game)
            runs.append([eng.apply(None, P.CMD_JOIN, {"name": n}, 5)[0]
                         .payload["player_id"] for n in ("a", "b", "c")])
        self.assertEqual(runs[0], runs[1])


class BuzzerTest(unittest.TestCase):
    UNLOCK = 10_000

    def test_press_before_unlock_is_early(self):
        status, _ = buzzer.classify(self.UNLOCK - 1, 0, self.UNLOCK, self.UNLOCK + 5)
        self.assertEqual(status, BUZZ_EARLY)

    def test_press_from_the_future_is_rejected(self):
        at = self.UNLOCK + 100
        status, _ = buzzer.classify(at + P.FUTURE_TOLERANCE_MS + 1, 0,
                                    self.UNLOCK, at)
        self.assertEqual(status, BUZZ_FUTURE)

    def test_slight_future_skew_is_tolerated(self):
        at = self.UNLOCK + 100
        status, _ = buzzer.classify(at + P.FUTURE_TOLERANCE_MS, 0, self.UNLOCK, at)
        self.assertEqual(status, BUZZ_ACCEPTED)

    def test_backdating_beyond_the_network_is_clamped(self):
        at = self.UNLOCK + 5_000
        rtt = 40
        floor = at - rtt - P.BACKDATE_MARGIN_MS
        status, press = buzzer.classify(self.UNLOCK + 1, rtt, self.UNLOCK, at)
        self.assertEqual(status, BUZZ_CLAMPED)
        self.assertEqual(press, floor)

    def test_a_clamped_buzz_still_races(self):
        at = self.UNLOCK + 5_000
        honest = Buzz("honest", at - 30, 0, 60, at, BUZZ_ACCEPTED)
        cheat = Buzz("cheat", at - 40 - P.BACKDATE_MARGIN_MS, 0, 40, at, BUZZ_CLAMPED)
        order = buzzer.resolve_order([honest, cheat], "J_1_1")
        self.assertEqual([b.player_id for b in order], ["cheat", "honest"])
        self.assertEqual(order[0].delta_ms, 0)

    def test_exact_ties_do_not_depend_on_arrival_order(self):
        def race(names):
            return [b.player_id for b in buzzer.resolve_order(
                [Buzz(n, 12_345, 0, 10, 12_400) for n in names], "J_4_2")]
        self.assertEqual(race(["alice", "bob", "carol"]),
                         race(["carol", "bob", "alice"]))

    def test_estimate_offset_uses_the_least_noisy_sample(self):
        # (t_send, t_server, t_recv): the 20ms round trip is the trustworthy one.
        offset, rtt = buzzer.estimate_offset([
            (1000, 5000, 1400),   # 400ms round trip, heavy queuing
            (2000, 6010, 2020),   # 20ms round trip
        ])
        self.assertEqual(rtt, 20)
        self.assertEqual(offset, 6010 + 10 - 2020)


class BuzzRaceTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.do(self.host, P.CMD_HOST_START_ROUND)
        self.unlock = self.open_clue(self.host, "J_2_1")

    def test_later_arrival_earlier_press_wins(self):
        """The whole reason the reconciliation window exists."""
        u = self.unlock
        # Bob is on a fast link and presses second; his packet lands first.
        self.buzz(self.bob, "J_2_1", press=u + 40, at=u + 55, rtt=15)
        # Alice pressed 30ms earlier but her packet needs 220ms to arrive.
        registered = self.buzz(self.alice, "J_2_1", press=u + 10, at=u + 230, rtt=220)
        first(registered, P.EVT_BUZZ_REGISTERED)

        result = first(self.tick(200), P.EVT_BUZZ_RESULT).payload
        self.assertEqual([r["player_id"] for r in result["order"]],
                         [self.alice, self.bob])
        self.assertEqual(result["order"][0]["delta_ms"], 0)
        self.assertEqual(result["order"][1]["delta_ms"], 30)

    def test_buzz_before_unlock_is_recorded_not_dropped(self):
        engine = self.engine
        engine.state.clue.step = P.STEP_READING
        engine.state.clue.unlock_at = self.unlock + 5_000
        self.buzz(self.alice, "J_2_1", press=self.unlock, at=self.unlock + 10)
        self.assertEqual([b.status for b in engine.state.clue.buzzes], [BUZZ_EARLY])

    def test_second_buzz_from_the_same_player_is_a_duplicate(self):
        u = self.unlock
        self.buzz(self.alice, "J_2_1", press=u + 10, at=u + 20)
        self.buzz(self.alice, "J_2_1", press=u + 1, at=u + 30)
        statuses = [b.status for b in self.engine.state.clue.buzzes]
        self.assertEqual(statuses, [BUZZ_ACCEPTED, BUZZ_DUPLICATE])
        result = first(self.tick(400), P.EVT_BUZZ_RESULT).payload
        self.assertEqual(len(result["order"]), 1)
        self.assertEqual(result["rejected"],
                         [{"player_id": self.alice, "status": BUZZ_DUPLICATE}])

    def test_wrong_then_right_walks_the_buzz_order(self):
        u = self.unlock
        value = self.game.clue("J_2_1").value
        self.buzz(self.alice, "J_2_1", press=u + 10, at=u + 20)
        self.buzz(self.bob, "J_2_1", press=u + 60, at=u + 70)
        self.tick(400)

        self.do(self.alice, P.CMD_SELF_REPORT, {"clue_id": "J_2_1", "correct": False})
        self.assertEqual(self.score(self.alice), -value)
        handoff = self.engine.state.clue
        self.assertEqual(handoff.order[handoff.order_index], self.bob)

        self.do(self.bob, P.CMD_SELF_REPORT, {"clue_id": "J_2_1", "correct": True})
        self.assertEqual(self.score(self.bob), value)
        self.assertEqual(self.engine.state.control, self.bob)

        resolved = self.resolve_pending().payload
        self.assertEqual(resolved["answer"], self.game.clue("J_2_1").answer)
        self.assertIn("J_2_1", self.engine.state.used)

    def test_only_the_holder_may_self_report(self):
        u = self.unlock
        self.buzz(self.alice, "J_2_1", press=u + 10, at=u + 20)
        self.tick(400)
        events = self.do(self.bob, P.CMD_SELF_REPORT,
                         {"clue_id": "J_2_1", "correct": True})
        self.assertEqual(types(events), [P.EVT_ERROR])
        self.assertEqual(self.score(self.bob), 0)

    def test_timeout_costs_the_turn_but_no_money(self):
        u = self.unlock
        self.buzz(self.alice, "J_2_1", press=u + 10, at=u + 20)
        self.tick(400)
        events = self.tick(P.ANSWER_TIMEOUT_MS + 1)
        self.assertEqual(first(events, P.EVT_ANSWER_RESULT).payload["score_delta"], 0)
        self.assertEqual(self.score(self.alice), 0)
        # Alice has spent her attempt; the clue reopens for everyone else.
        first(events, P.EVT_BUZZERS_OPEN)
        self.assertEqual(self.engine.state.clue.attempted, [self.alice])

        retry = self.buzz(self.alice, "J_2_1", press=self.now + 1, at=self.now + 5)
        self.assertEqual(types(retry), [P.EVT_ERROR])

    def test_everyone_missing_resolves_the_clue(self):
        u = self.unlock
        for pid in (self.alice, self.bob, self.host):
            self.buzz(pid, "J_2_1", press=u + 10, at=u + 20)
        self.tick(400)
        for _ in range(3):
            holder = self.engine.state.clue.order[self.engine.state.clue.order_index]
            self.do(holder, P.CMD_SELF_REPORT, {"clue_id": "J_2_1", "correct": False})
        self.assertEqual(self.resolve_pending().payload["clue_id"], "J_2_1")


class DailyDoubleTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.do(self.host, P.CMD_HOST_START_ROUND)

    def test_selection_reveals_it_to_everyone_at_once(self):
        events = self.do(self.host, P.CMD_SELECT_CLUE, {"clue_id": J_DD})
        revealed = first(events, P.EVT_DD_REVEALED)
        self.assertIsNone(revealed.targets, "a Daily Double is never a private fact")
        self.assertEqual(revealed.payload["min_wager"], P.DD_MIN_WAGER)
        # A broke selector may still wager up to the top value of the board.
        self.assertEqual(revealed.payload["max_wager"], 1000)

    def test_nobody_else_may_wager_or_buzz(self):
        self.do(self.host, P.CMD_SELECT_CLUE, {"clue_id": J_DD})
        self.assertEqual(types(self.do(self.alice, P.CMD_DD_WAGER, {"amount": 100})),
                         [P.EVT_ERROR])
        self.assertEqual(types(self.buzz(self.alice, J_DD, self.now, self.now)),
                         [P.EVT_ERROR])

    def test_wager_bounds_are_enforced(self):
        self.do(self.host, P.CMD_SELECT_CLUE, {"clue_id": J_DD})
        for bad in (P.DD_MIN_WAGER - 1, 1001):
            self.assertEqual(types(self.do(self.host, P.CMD_DD_WAGER,
                                           {"amount": bad})), [P.EVT_ERROR])

    def test_correct_daily_double_pays_the_wager(self):
        self.do(self.host, P.CMD_SELECT_CLUE, {"clue_id": J_DD})
        events = self.do(self.host, P.CMD_DD_WAGER, {"amount": 800})
        self.assertEqual(first(events, P.EVT_DD_WAGER_SET).payload["amount"], 800)
        self.do(self.host, P.CMD_SELF_REPORT, {"clue_id": J_DD, "correct": True})
        self.assertEqual(self.score(self.host), 800)
        self.assertEqual(self.resolve_pending().payload["answer"],
                         self.game.clue(J_DD).answer)
        self.assertEqual(self.engine.state.control, self.host)


class HostPowersTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.do(self.host, P.CMD_HOST_START_ROUND)

    def test_adjust_then_undo_leaves_the_score_where_it_started(self):
        before = self.score(self.alice)
        adjusted = first(self.do(self.host, P.CMD_HOST_ADJUST_SCORE, {
            "player_id": self.alice, "delta": 400,
            "reason": "room agreed it counted"}), P.EVT_SCORE_ADJUSTED)
        self.assertEqual(adjusted.payload["score"], before + 400)
        undone = first(self.do(self.host, P.CMD_HOST_UNDO), P.EVT_SCORE_ADJUSTED)
        self.assertEqual(undone.payload["delta"], -400)
        self.assertEqual(self.score(self.alice), before)

    def test_undo_reverses_a_self_reported_answer(self):
        unlock = self.open_clue(self.host, "J_2_1")
        value = self.game.clue("J_2_1").value
        self.buzz(self.bob, "J_2_1", press=unlock + 5, at=unlock + 10)
        self.tick(400)
        self.do(self.bob, P.CMD_SELF_REPORT, {"clue_id": "J_2_1", "correct": True})
        self.assertEqual(self.score(self.bob), value)
        self.do(self.host, P.CMD_HOST_UNDO)
        self.assertEqual(self.score(self.bob), 0)

    def test_host_commands_are_refused_to_players(self):
        self.assertEqual(types(self.do(self.alice, P.CMD_HOST_ADJUST_SCORE, {
            "player_id": self.alice, "delta": 9999, "reason": "nice try"})),
            [P.EVT_ERROR])
        self.assertEqual(self.score(self.alice), 0)

    def test_set_control_and_kick(self):
        self.do(self.host, P.CMD_HOST_SET_CONTROL, {"player_id": self.bob})
        self.assertEqual(self.engine.state.control, self.bob)
        self.do(self.host, P.CMD_HOST_KICK, {"player_id": self.bob})
        self.assertNotIn(self.bob, self.engine.state.players)
        self.assertNotEqual(self.engine.state.control, self.bob)

    def test_selection_requires_control(self):
        self.assertEqual(types(self.do(self.alice, P.CMD_SELECT_CLUE,
                                       {"clue_id": "J_3_1"})), [P.EVT_ERROR])

    def test_force_resolve_discloses_the_answer_and_spends_the_cell(self):
        self.open_clue(self.host, "J_3_1")
        self.do(self.host, P.CMD_HOST_FORCE_RESOLVE)
        resolved = self.resolve_pending().payload
        self.assertEqual(resolved["answer"], self.game.clue("J_3_1").answer)
        self.assertIn("J_3_1", self.engine.state.used)


class ReturnToBoardTest(EngineFixture):
    """The answer screen is shared state: it stays up until the host closes it."""

    def setUp(self):
        super().setUp()
        self.do(self.host, P.CMD_HOST_START_ROUND)
        self.open_clue(self.host, "J_3_1")

    def resolve(self):
        self.do(self.host, P.CMD_HOST_FORCE_RESOLVE)
        return self.resolve_pending()

    def test_a_resolved_clue_holds_the_board_until_the_host_returns(self):
        self.resolve()
        self.assertEqual(types(self.do(self.host, P.CMD_SELECT_CLUE,
                                       {"clue_id": "J_3_2"})), [P.EVT_ERROR])
        self.assertEqual(self.return_to_board().payload, {"clue_id": "J_3_1"})
        self.assertIsNone(self.engine.state.clue)
        self.assertEqual(types(self.do(self.host, P.CMD_SELECT_CLUE,
                                       {"clue_id": "J_3_2"})), [P.EVT_CLUE_SELECTED])

    def test_only_the_host_may_return_to_the_board(self):
        self.resolve()
        self.assertEqual(types(self.do(self.alice, P.CMD_HOST_RETURN_TO_BOARD)),
                         [P.EVT_ERROR])
        self.assertIsNotNone(self.engine.state.clue)

    def test_returning_before_resolution_is_refused(self):
        self.assertEqual(types(self.do(self.host, P.CMD_HOST_RETURN_TO_BOARD)),
                         [P.EVT_ERROR])
        self.assertEqual(self.engine.state.clue.step, P.STEP_OPEN)

    def test_a_resolved_clue_cannot_be_resolved_again(self):
        self.resolve()
        self.assertEqual(types(self.do(self.host, P.CMD_HOST_FORCE_RESOLVE)),
                         [P.EVT_ERROR])
        self.assertEqual(self.engine.tick(self.now + 10_000), [])

    def test_snapshot_shows_the_answer_screen_only_once_resolved(self):
        self.do(self.host, P.CMD_HOST_FORCE_RESOLVE)
        pending = self.engine.snapshot_for(self.alice)["clue"]
        self.assertIsNone(pending["resolved_answer"])
        self.resolve_pending()
        clue = self.engine.snapshot_for(self.alice)["clue"]
        self.assertEqual(clue["step"], P.STEP_RESOLVED)
        self.assertEqual(clue["resolved_answer"], self.game.clue("J_3_1").answer)
        self.return_to_board()
        self.assertIsNone(self.engine.snapshot_for(self.alice)["clue"])

    def test_next_round_leaves_the_answer_screen_too(self):
        self.resolve()
        first(self.do(self.host, P.CMD_HOST_NEXT_ROUND), P.EVT_ROUND_STARTED)
        self.assertIsNone(self.engine.state.clue)


class ScriptedGameTest(EngineFixture):
    """One game end to end: both rounds, a Daily Double, a miss chain, a timeout,
    a host correction, an undo, and Final Jeopardy."""

    def play(self, selector, clue_id, winner, correct=True):
        unlock = self.open_clue(selector, clue_id)
        self.buzz(winner, clue_id, press=unlock + 10, at=unlock + 20)
        self.tick(P.BUZZ_WINDOW_MS + 10)
        self.do(winner, P.CMD_SELF_REPORT,
                {"clue_id": clue_id, "correct": correct})
        resolved = self.resolve_pending()
        self.return_to_board()
        return resolved

    def test_full_game(self):
        self.do(self.host, P.CMD_HOST_START_ROUND)
        self.assertEqual(self.engine.state.phase, P.PHASE_ROUND)
        self.assertEqual(self.engine.state.round, "J")

        # --- Jeopardy round: a clean pickup, then a miss chain.
        self.play(self.host, "J_2_1", self.alice)
        self.assertEqual(self.score(self.alice), 200)

        unlock = self.open_clue(self.alice, "J_2_2")
        self.buzz(self.bob, "J_2_2", press=unlock + 80, at=unlock + 90)
        self.buzz(self.host, "J_2_2", press=unlock + 20, at=unlock + 240, rtt=210)
        order = first(self.tick(200), P.EVT_BUZZ_RESULT).payload["order"]
        self.assertEqual([o["player_id"] for o in order], [self.host, self.bob])
        self.do(self.host, P.CMD_SELF_REPORT, {"clue_id": "J_2_2", "correct": False})
        self.do(self.bob, P.CMD_SELF_REPORT, {"clue_id": "J_2_2", "correct": True})
        self.resolve_pending()
        self.return_to_board()
        self.assertEqual(self.score(self.host), -400)
        self.assertEqual(self.score(self.bob), 400)

        # --- A timeout: the buzz holder never speaks, so the clue costs nothing.
        unlock = self.open_clue(self.bob, "J_2_3")
        self.buzz(self.bob, "J_2_3", press=unlock + 5, at=unlock + 15)
        self.tick(P.BUZZ_WINDOW_MS + 10)
        timed_out = self.tick(P.ANSWER_TIMEOUT_MS + 1)
        self.assertEqual(first(timed_out, P.EVT_ANSWER_RESULT)
                         .payload["score_delta"], 0)
        self.assertEqual(self.score(self.bob), 400)
        self.do(self.host, P.CMD_HOST_FORCE_RESOLVE)
        self.resolve_pending()
        self.return_to_board()

        # --- Daily Double: the selector alone plays it.
        self.do(self.host, P.CMD_HOST_SET_CONTROL, {"player_id": self.alice})
        self.do(self.alice, P.CMD_SELECT_CLUE, {"clue_id": J_DD})
        self.do(self.alice, P.CMD_DD_WAGER, {"amount": 1000})
        self.do(self.alice, P.CMD_SELF_REPORT, {"clue_id": J_DD, "correct": True})
        self.resolve_pending()
        self.assertEqual(self.score(self.alice), 1200)

        # --- A host correction the room disputes, then takes back.
        self.do(self.host, P.CMD_HOST_ADJUST_SCORE, {
            "player_id": self.host, "delta": 400, "reason": "misheard"})
        self.assertEqual(self.score(self.host), 0)
        self.do(self.host, P.CMD_HOST_UNDO)
        self.assertEqual(self.score(self.host), -400)
        self.do(self.host, P.CMD_HOST_ADJUST_SCORE, {
            "player_id": self.host, "delta": 900, "reason": "room vote"})

        # --- Double Jeopardy.
        started = first(self.do(self.host, P.CMD_HOST_NEXT_ROUND),
                        P.EVT_ROUND_STARTED)
        self.assertEqual(started.payload["round"], "DJ")
        self.assertEqual(len(started.payload["cells"]), 30)
        self.play(self.engine.state.control, "DJ_3_1", self.bob)
        self.assertEqual(self.score(self.bob), 800)

        # --- Final Jeopardy.
        category = first(self.do(self.host, P.CMD_HOST_NEXT_ROUND),
                         P.EVT_FINAL_CATEGORY).payload["category"]
        self.assertEqual(category, self.game.clue("FJ").category)
        self.assertEqual(self.engine.state.phase, P.PHASE_FINAL)

        opened = first(self.do(self.host, P.CMD_HOST_NEXT_ROUND),
                       P.EVT_FINAL_WAGERS_OPEN).payload
        self.assertEqual(sorted(opened["participants"]),
                         sorted([self.host, self.alice, self.bob]))

        # Amounts chosen not to collide with any score already on the wire, so
        # the seal check below cannot pass by coincidence.
        wagers = {self.host: 499, self.alice: 1111, self.bob: 777}
        for pid, amount in wagers.items():
            received = self.do(pid, P.CMD_FINAL_WAGER, {"amount": amount})
            self.assertEqual(first(received, P.EVT_FINAL_WAGER_RECEIVED)
                             .payload, {"player_id": pid})
            # Nothing announces the amount, and no other player can read it.
            other = self.host if pid != self.host else self.alice
            self.assertNotIn(str(amount),
                             json.dumps(self.engine.snapshot_for(other)))
        # The last wager opens the clue automatically.
        self.assertEqual(self.engine.state.final.step, P.FINAL_ANSWERS)

        for pid in (self.host, self.alice):
            self.do(pid, P.CMD_FINAL_ANSWER, {"text": "who is grendel"})
        self.assertNotIn("grendel", json.dumps(self.engine.snapshot_for(self.bob)))
        reveal = first(self.do(self.bob, P.CMD_FINAL_ANSWER, {"text": "beowulf"}),
                       P.EVT_FINAL_REVEAL).payload
        self.assertEqual(reveal["answer"], self.game.clue("FJ").answer)
        self.assertEqual({e["player_id"]: e["wager"] for e in reveal["entries"]},
                         wagers)

        self.do(self.host, P.CMD_FINAL_REPORT, {"correct": True})
        self.do(self.alice, P.CMD_FINAL_REPORT, {"correct": True})
        over = first(self.do(self.bob, P.CMD_FINAL_REPORT, {"correct": False}),
                     P.EVT_GAME_OVER).payload
        self.assertEqual(self.engine.state.phase, P.PHASE_GAME_OVER)
        self.assertEqual(over["scores"], {self.host: 999, self.alice: 2311,
                                          self.bob: 23})
        self.assertEqual(over["winner"], self.alice)


class FinalSealTest(EngineFixture):
    def setUp(self):
        super().setUp()
        self.do(self.host, P.CMD_HOST_START_ROUND)
        for pid, delta in ((self.alice, 1000), (self.bob, 600)):
            self.do(self.host, P.CMD_HOST_ADJUST_SCORE,
                    {"player_id": pid, "delta": delta, "reason": "seed"})
        self.do(self.host, P.CMD_HOST_NEXT_ROUND)   # DJ
        self.do(self.host, P.CMD_HOST_NEXT_ROUND)   # Final: category
        self.do(self.host, P.CMD_HOST_NEXT_ROUND)   # Final: wagers open

    def test_broke_players_do_not_play_final(self):
        self.assertNotIn(self.host, self.engine.state.final.participants)
        self.assertEqual(types(self.do(self.host, P.CMD_FINAL_WAGER,
                                       {"amount": 0})), [P.EVT_ERROR])

    def test_wager_cannot_exceed_score(self):
        self.assertEqual(types(self.do(self.bob, P.CMD_FINAL_WAGER,
                                       {"amount": 601})), [P.EVT_ERROR])

    def test_silence_at_the_wager_deadline_still_opens_the_clue(self):
        self.do(self.alice, P.CMD_FINAL_WAGER, {"amount": 1000})
        events = self.tick(P.FINAL_ANSWER_MS + 1)
        first(events, P.EVT_FINAL_CLUE)
        self.assertEqual(self.engine.state.final.step, P.FINAL_ANSWERS)

    def test_answer_deadline_reveals_what_was_submitted(self):
        for pid in (self.alice, self.bob):
            self.do(pid, P.CMD_FINAL_WAGER, {"amount": 100})
        self.do(self.alice, P.CMD_FINAL_ANSWER, {"text": "who is grendel"})
        reveal = first(self.tick(P.FINAL_ANSWER_MS + 1), P.EVT_FINAL_REVEAL).payload
        entries = {e["player_id"]: e["answer"] for e in reveal["entries"]}
        self.assertEqual(entries[self.bob], "")
        self.assertEqual(entries[self.alice], "who is grendel")


class ReplayTest(EngineFixture):
    """store.py replays the journal; the same commands must rebuild the same state."""

    JOURNAL = [
        (None, P.CMD_JOIN, {"name": "host"}, 0),
        (None, P.CMD_JOIN, {"name": "alice"}, 10),
        ("p1", P.CMD_HOST_START_ROUND, {}, 20),
        ("p1", P.CMD_SELECT_CLUE, {"clue_id": "J_2_1"}, 30),
        ("p1", P.CMD_HOST_FORCE_RESOLVE, {}, 40),
        ("p1", P.CMD_HOST_RETURN_TO_BOARD, {}, 50),
        ("p1", P.CMD_SELECT_CLUE, {"clue_id": "J_2_2"}, 60),
    ]

    def replay(self):
        engine = Engine(Game.from_dict(GAME_DICT))
        for actor, ctype, fields, now in self.JOURNAL:
            engine.apply(actor, ctype, fields, now)
        return engine

    def test_replay_is_byte_identical(self):
        a, b = self.replay(), self.replay()
        self.assertEqual(a.state.clue.clue_id, "J_2_2")
        self.assertEqual(json.dumps(a.state.to_dict(), sort_keys=True),
                         json.dumps(b.state.to_dict(), sort_keys=True))

    def test_state_round_trips_through_json(self):
        engine = self.replay()
        blob = json.dumps(engine.state.to_dict(), sort_keys=True)
        restored = GameState.from_dict(json.loads(blob))
        self.assertEqual(json.dumps(restored.to_dict(), sort_keys=True), blob)


if __name__ == "__main__":
    unittest.main()
