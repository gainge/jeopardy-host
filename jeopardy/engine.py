"""Authoritative game rules as a pure state machine.

PURITY CONTRACT — enforced by tests/test_engine.py:
  * no imports of time, socket, http, threading, or random
  * no file or network I/O
  * every time-dependent decision takes `now_ms` as an argument

The transport owns the clock and calls `tick()` when `next_deadline()` comes due.
Because the engine is deterministic, the command journal in store.py can replay a
game exactly, which is what makes crash recovery and host undo work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Buzz, Game, Player


@dataclass
class Event:
    """One thing that happened. `targets=None` means broadcast to everyone."""
    type: str
    payload: dict[str, Any]
    targets: list[str] | None = None


@dataclass
class ClueState:
    clue_id: str
    step: str
    unlock_at: int | None = None
    window_closes_at: int | None = None
    answer_deadline: int | None = None
    buzzes: list[Buzz] = field(default_factory=list)
    order: list[str] = field(default_factory=list)   # resolved buzz order
    order_index: int = 0
    attempted: list[str] = field(default_factory=list)
    is_daily_double: bool = False
    dd_player: str | None = None
    dd_wager: int | None = None


@dataclass
class FinalState:
    step: str
    wagers: dict[str, int] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    deadline: int | None = None


@dataclass
class GameState:
    phase: str
    round: str | None = None
    players: dict[str, Player] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)      # stable seating order
    used: list[str] = field(default_factory=list)       # spent clue ids
    control: str | None = None
    clue: ClueState | None = None
    final: FinalState | None = None

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GameState": ...


class Engine:
    """Applies commands to state and returns the events that resulted."""

    def __init__(self, game: Game, state: GameState | None = None) -> None:
        self.game = game
        self.state = state or GameState(phase="lobby")

    def apply(self, actor_id: str | None, ctype: str, fields: dict[str, Any],
              now_ms: int) -> list[Event]:
        """Apply one validated command. Rule violations return an EVT_ERROR event
        targeted at the actor rather than raising."""
        raise NotImplementedError

    def tick(self, now_ms: int) -> list[Event]:
        """Fire any deadline that has come due (unlock, buzz window, timeout)."""
        raise NotImplementedError

    def next_deadline(self) -> int | None:
        """Earliest pending deadline in server ms, or None if nothing is scheduled."""
        raise NotImplementedError

    def snapshot_for(self, player_id: str) -> dict[str, Any]:
        """Full redacted state for one recipient. Must route through redact.py."""
        raise NotImplementedError
