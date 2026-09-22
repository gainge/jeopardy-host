"""The spoiler boundary. Nothing reaches a client except through this module.

The host plays too, so there is no privileged recipient: the host's payloads are
redacted exactly like everyone else's. A clue's `answer` appears only once the
engine has marked it disclosed, and `is_daily_double` only once the cell has been
selected. tests/test_redact.py walks every serialized payload for every recipient
and fails if either ever leaks early.
"""

from __future__ import annotations

from typing import Any

from .model import Game

# Keys that must never appear in a client payload unless explicitly disclosed.
SECRET_KEYS = ("answer", "is_daily_double")


def public_clue(game: Game, cid: str, *, disclose_answer: bool = False,
                disclose_dd: bool = False) -> dict[str, Any]:
    """A clue trimmed to what the recipient is currently allowed to know."""
    raise NotImplementedError


def public_board(game: Game, round_name: str, used: list[str]) -> dict[str, Any]:
    """Categories plus each cell's id/value/availability. Carries no clue text."""
    raise NotImplementedError


def assert_no_leak(payload: Any) -> None:
    """Recursively assert a payload carries no undisclosed secret. Used in tests
    and as a cheap runtime guard on the broadcast path."""
    raise NotImplementedError
