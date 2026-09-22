"""j-archive.com -> Game.

Structure this relies on (verified against game 9537):
  * clue text   td#clue_{J|DJ}_{col}_{row}.clue_text
  * answer      the sibling td#clue_..._r, inside <em class="correct_response">
  * daily double the header cell's class is clue_value_daily_double, and its text
                is the contestant's WAGER, not the board value — so the cell value
                is always derived from row position via model.ROW_VALUES
  * final       td#clue_FJ / td#clue_FJ_r, category in table.final_round
  * media       absolute links to j-archive.com/media/*, downloaded locally so
                gameplay needs no internet

The answer cell also holds judge banter and contestant names; only the
correct_response span is extracted.
"""

from __future__ import annotations

from .model import Game


class ScrapeError(RuntimeError):
    """The page was missing clues or otherwise unusable. Fail loudly at scrape
    time rather than discovering a hole mid-game."""


def parse_game(html_text: str, source_url: str = "") -> Game:
    """Parse a saved showgame.php page. Pure — no network, so it is fixture-testable."""
    raise NotImplementedError


def fetch_game(url_or_id: str, media_dir: str | None = None) -> Game:
    """Fetch via urllib, parse, and optionally download media alongside."""
    raise NotImplementedError
