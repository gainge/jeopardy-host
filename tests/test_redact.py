"""Phase 2 acceptance gate: the spoiler boundary and the engine purity contract.

The host plays too, so there is no privileged recipient. These tests drive a game
forward and assert that no payload bound for *any* player — host included — ever
carries a clue answer or a Daily Double marker before the engine discloses it.
"""

import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy import protocol as P  # noqa: E402
from jeopardy.engine import Engine  # noqa: E402
from jeopardy.model import Game  # noqa: E402
from jeopardy.redact import assert_no_leak  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "fixtures", "expected_9537.json")

# Every answer and every Daily Double location in the fixture game. Any of these
# strings appearing in a payload before disclosure is a spoiler.
with open(GOLDEN, encoding="utf-8") as _fh:
    GAME_DICT = json.load(_fh)
ALL_ANSWERS = [c["answer"] for r in GAME_DICT["rounds"].values()
               for c in r["clues"].values()]
DD_IDS = {c["id"] for r in GAME_DICT["rounds"].values()
          for c in r["clues"].values() if c["is_daily_double"]}


def load_game() -> Game:
    return Game.from_dict(GAME_DICT)


def scan(payload, allowed: set) -> list:
    """Return every answer string present in `payload` that is not yet allowed."""
    blob = json.dumps(payload, ensure_ascii=False)
    return [a for a in ALL_ANSWERS if a not in allowed and len(a) > 3 and a in blob]


class PurityTest(unittest.TestCase):
    """The engine must stay clockless and I/O-free, or replay and testing break."""

    FORBIDDEN = {"time", "socket", "http", "threading", "random", "urllib",
                 "datetime", "os", "secrets"}

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
        leaked = imported & self.FORBIDDEN
        self.assertFalse(leaked, f"engine.py must stay pure; found {sorted(leaked)}")


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.game = load_game()
        self.engine = Engine(self.game)
        self.now = 1_000_000
        self.ids = {}
        for name in ("host", "alice", "bob"):
            evts = self.engine.apply(None, P.CMD_JOIN, {"name": name}, self.now)
            self.ids[name] = evts[0].payload["player_id"]
        self.host = self.ids["host"]

    def snapshots(self):
        return {pid: self.engine.snapshot_for(pid) for pid in self.ids.values()}

    def test_lobby_and_board_leak_nothing(self):
        self.engine.apply(self.host, P.CMD_HOST_START_ROUND, {}, self.now)
        for pid, snap in self.snapshots().items():
            assert_no_leak(snap)
            self.assertEqual(scan(snap, set()), [], f"answer leaked to {pid}")
            self.assertNotIn("is_daily_double", json.dumps(snap))

    def test_host_snapshot_is_not_privileged(self):
        self.engine.apply(self.host, P.CMD_HOST_START_ROUND, {}, self.now)
        snaps = self.snapshots()
        board = lambda s: json.dumps(s.get("board"), sort_keys=True)
        self.assertEqual(board(snaps[self.host]), board(snaps[self.ids["alice"]]),
                         "the host must see exactly the board a player sees")

    def test_daily_double_hidden_until_selected(self):
        self.engine.apply(self.host, P.CMD_HOST_START_ROUND, {}, self.now)
        dd = sorted(DD_IDS & {f"J_{c}_{r}" for c in range(1, 7) for r in range(1, 6)})[0]
        # Selecting a plain cell must not reveal where the Daily Double is.
        plain = "J_2_1" if dd != "J_2_1" else "J_3_1"
        for evt in self.engine.apply(self.host, P.CMD_SELECT_CLUE,
                                     {"clue_id": plain}, self.now):
            assert_no_leak(evt.payload)
            self.assertNotIn(dd, json.dumps(evt.payload))

    def test_answer_disclosed_only_on_resolution(self):
        self.engine.apply(self.host, P.CMD_HOST_START_ROUND, {}, self.now)
        target = "J_2_1"
        answer = self.game.clue(target).answer
        events = self.engine.apply(self.host, P.CMD_SELECT_CLUE,
                                   {"clue_id": target}, self.now)
        for evt in events:
            self.assertNotIn(answer, json.dumps(evt.payload),
                             "answer leaked at selection time")
        self.now += P.READ_MS_MAX + 1
        self.engine.tick(self.now)
        self.engine.apply(self.host, P.CMD_HOST_FORCE_RESOLVE, {}, self.now)
        resolved = [e for e in self.engine.tick(self.now) + self.engine.apply(
            self.host, P.CMD_HOST_FORCE_RESOLVE, {}, self.now)
            if e.type == P.EVT_CLUE_RESOLVED]
        blob = json.dumps([e.payload for e in resolved])
        self.assertIn(answer, blob, "the answer must be disclosed on resolution")

    def test_final_wagers_and_answers_stay_sealed(self):
        self.engine.apply(self.host, P.CMD_HOST_START_ROUND, {}, self.now)
        # Drive to Final via the host, then seal-check one player's wager.
        self.engine.apply(self.host, P.CMD_HOST_NEXT_ROUND, {}, self.now)
        self.engine.apply(self.host, P.CMD_HOST_NEXT_ROUND, {}, self.now)
        alice = self.ids["alice"]
        self.engine.apply(alice, P.CMD_FINAL_WAGER, {"amount": 1234}, self.now)
        other = self.engine.snapshot_for(self.ids["bob"])
        self.assertNotIn("1234", json.dumps(other),
                         "another player's Final wager must stay sealed")


if __name__ == "__main__":
    unittest.main()
