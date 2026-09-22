"""Core data types for a scraped game and for live game state.

Every type here round-trips through plain JSON (`to_dict`/`from_dict`) because the
event log, the snapshot wire format, and the scraped game artifact all share these
shapes. Keep them dumb: no behavior beyond serialization lives here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any

ROUND_J = "J"
ROUND_DJ = "DJ"
ROUND_FJ = "FJ"
ROUNDS = (ROUND_J, ROUND_DJ, ROUND_FJ)

# Board value per row, by round. Daily Double cells on j-archive display the
# contestant's wager rather than the cell value, so value is always derived
# from row position and never parsed from the page.
ROW_VALUES = {
    ROUND_J: (200, 400, 600, 800, 1000),
    ROUND_DJ: (400, 800, 1200, 1600, 2000),
}

BOARD_COLS = 6
BOARD_ROWS = 5


def clue_id(round_name: str, col: int, row: int) -> str:
    """Canonical clue identifier. Matches j-archive's own scheme minus the prefix."""
    if round_name == ROUND_FJ:
        return ROUND_FJ
    return f"{round_name}_{col}_{row}"


@dataclass
class Clue:
    id: str
    round: str
    col: int
    row: int
    category: str
    text: str
    answer: str
    value: int
    is_daily_double: bool = False
    media: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Clue":
        return cls(
            id=d["id"],
            round=d["round"],
            col=d["col"],
            row=d["row"],
            category=d["category"],
            text=d["text"],
            answer=d["answer"],
            value=d["value"],
            is_daily_double=d.get("is_daily_double", False),
            media=list(d.get("media", [])),
        )


@dataclass
class Round:
    name: str
    categories: list[str]
    clues: dict[str, Clue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "categories": list(self.categories),
            "clues": {k: v.to_dict() for k, v in self.clues.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Round":
        return cls(
            name=d["name"],
            categories=list(d["categories"]),
            clues={k: Clue.from_dict(v) for k, v in d["clues"].items()},
        )


@dataclass
class Game:
    game_id: str
    title: str
    aired: str
    source_url: str
    rounds: dict[str, Round]

    def clue(self, cid: str) -> Clue:
        for rnd in self.rounds.values():
            if cid in rnd.clues:
                return rnd.clues[cid]
        raise KeyError(cid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "title": self.title,
            "aired": self.aired,
            "source_url": self.source_url,
            "rounds": {k: v.to_dict() for k, v in self.rounds.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Game":
        return cls(
            game_id=d["game_id"],
            title=d["title"],
            aired=d["aired"],
            source_url=d["source_url"],
            rounds={k: Round.from_dict(v) for k, v in d["rounds"].items()},
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Game":
        return cls.from_dict(json.loads(text))


@dataclass
class Player:
    id: str
    name: str
    score: int = 0
    connected: bool = True
    is_host: bool = False
    # Diagnostics surfaced in the host panel so a misbehaving clock is visible.
    rtt_ms: int = 0
    offset_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Player":
        return cls(**d)


# --- Buzz records -----------------------------------------------------------

BUZZ_ACCEPTED = "accepted"
BUZZ_EARLY = "early"      # pressed before unlock; ignored, player may re-press
BUZZ_FUTURE = "future"    # claimed a press time the server has not reached
BUZZ_CLAMPED = "clamped"  # backdated further than the network could explain
BUZZ_DUPLICATE = "duplicate"


@dataclass
class Buzz:
    player_id: str
    press_server: int   # client press time translated into server milliseconds
    offset_ms: int      # client's own clock-offset estimate, for auditing
    rtt_ms: int         # client's own round-trip estimate, for auditing
    received_at: int    # server wall-clock ms when the POST landed
    status: str = BUZZ_ACCEPTED
    delta_ms: int = 0   # ms behind the winning press; filled in on resolution

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Buzz":
        return cls(**d)
