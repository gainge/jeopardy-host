"""Phase 1 acceptance gate.

The golden file is a regression net; the explicit assertions below are the actual
spec, hand-verified against the raw HTML of game 9537.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy.model import Game, ROUND_DJ, ROUND_FJ, ROUND_J  # noqa: E402
from jeopardy.scraper import parse_game  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "game_9537.html")
GOLDEN = os.path.join(HERE, "fixtures", "expected_9537.json")
SOURCE_URL = "https://j-archive.com/showgame.php?game_id=9537"


class ScraperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, encoding="utf-8") as fh:
            cls.game = parse_game(fh.read(), SOURCE_URL)

    def test_metadata(self):
        # game_id is j-archive's internal id and is deliberately distinct from the
        # show number that appears in the title.
        self.assertEqual(self.game.game_id, "9537")
        self.assertEqual(self.game.aired, "2026-09-18")
        self.assertIn("9620", self.game.title)

    def test_board_is_complete(self):
        for rnd in (ROUND_J, ROUND_DJ):
            self.assertEqual(len(self.game.rounds[rnd].categories), 6, rnd)
            self.assertEqual(len(self.game.rounds[rnd].clues), 30, rnd)
            for clue in self.game.rounds[rnd].clues.values():
                self.assertTrue(clue.text.strip(), clue.id)
                self.assertTrue(clue.answer.strip(), clue.id)

    def test_categories(self):
        self.assertEqual(
            self.game.rounds[ROUND_J].categories,
            ["RECAPS OF SHAKESPEARE", "TALK LIKE A PIRATE", "SCIENCE",
             "FOOD & DRINK", "FALSE BERLIN", "BIZARRE LITTLE MEN"],
        )
        self.assertEqual(self.game.rounds[ROUND_DJ].categories[0], "MIDTERM ELECTIONS")

    def test_daily_doubles(self):
        found = sorted(c.id for rnd in (ROUND_J, ROUND_DJ)
                       for c in self.game.rounds[rnd].clues.values() if c.is_daily_double)
        self.assertEqual(found, ["DJ_1_3", "DJ_2_3", "J_1_4"])

    def test_daily_double_value_comes_from_row_not_page(self):
        # The page renders this cell as "DD: $1,200" (the contestant's wager).
        # Row 4 of the Jeopardy round is worth $800.
        self.assertEqual(self.game.rounds[ROUND_J].clues["J_1_4"].value, 800)
        self.assertEqual(self.game.rounds[ROUND_DJ].clues["DJ_1_3"].value, 1200)

    def test_values_follow_row_position(self):
        for cid, expected in (("J_1_1", 200), ("J_3_5", 1000),
                              ("DJ_1_1", 400), ("DJ_6_5", 2000)):
            self.assertEqual(self.game.clue(cid).value, expected, cid)

    def test_answers_exclude_judge_banter(self):
        # J_2_1's answer cell also contains "(Holly: What is arrr-chipelago?)"
        # and "(Ken: Nicely done.)". Only the correct_response span counts.
        self.assertEqual(self.game.clue("J_2_1").answer, "archipelago")
        self.assertEqual(self.game.clue("J_1_1").answer, "Othello")
        # Italic markup inside the response is stripped, not preserved.
        self.assertEqual(self.game.clue("J_1_4").answer, "The Two Gentlemen of Verona")

    def test_media_links_captured(self):
        clue = self.game.clue("J_6_1")
        self.assertEqual(len(clue.media), 1)
        self.assertTrue(clue.media[0].endswith("2026-09-18_J_30.jpg"))
        # The anchor wraps a word of the clue; that word survives in the text.
        self.assertTrue(clue.text.startswith("They don't just make toys"))

    def test_final_jeopardy(self):
        fj = self.game.rounds[ROUND_FJ].clues["FJ"]
        self.assertEqual(fj.category, "FICTIONAL CHARACTERS")
        self.assertEqual(fj.answer, "Grendel")
        self.assertIn("Heorot", fj.text)
        self.assertEqual(fj.value, 0)

    def test_matches_golden(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            expected = json.load(fh)
        self.assertEqual(json.loads(self.game.to_json()), expected)

    def test_round_trips_through_json(self):
        self.assertEqual(Game.from_json(self.game.to_json()).to_json(),
                         self.game.to_json())


if __name__ == "__main__":
    unittest.main()
