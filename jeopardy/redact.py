"""The spoiler boundary. Nothing reaches a client except through this module.

The host plays too, so there is no privileged recipient: the host's payloads are
redacted exactly like everyone else's. A clue's `answer` appears only once the
engine has marked it disclosed, and `is_daily_double` only once the cell has been
selected. tests/test_redact.py walks every serialized payload for every recipient
and fails if either ever leaks early.

One sanctioned early-disclosure channel exists: the current answer holder may be
shown the real answer, at host discretion, before self-report (engine.py's
`_reveal_answer`). That value travels under `your_revealed_answer`, never the bare
`answer` key, the same way Final Jeopardy's own wager/answer already travel under
`your_wager`/`your_answer` — so it never trips the `SECRET_KEYS`/`SEALED_KEYS`
guard below, and never reaches anyone but the player it was revealed to.
"""

from __future__ import annotations

from typing import Any

from .model import BOARD_COLS, BOARD_ROWS, Game

# Keys that must never appear in a client payload unless explicitly disclosed.
SECRET_KEYS = ("answer", "is_daily_double")

# Sealed until Final Jeopardy reveals everything at once.
SEALED_KEYS = ("wager", "wagers", "final_answer")


class SpoilerError(AssertionError):
    """A payload carried a secret the recipient has not earned yet."""


def public_clue(game: Game, cid: str, *, disclose_answer: bool = False,
                disclose_dd: bool = False) -> dict[str, Any]:
    """A clue trimmed to what the recipient is currently allowed to know."""
    clue = game.clue(cid)
    out: dict[str, Any] = {
        "clue_id": clue.id,
        "category": clue.category,
        "value": clue.value,
        "text": clue.text,
        "media": list(clue.media),
    }
    if disclose_answer:
        out["answer"] = clue.answer
    if disclose_dd:
        out["is_daily_double"] = clue.is_daily_double
    return out


def public_board(game: Game, round_name: str, used: list[str]) -> dict[str, Any]:
    """Categories plus each cell's id/value/availability. Carries no clue text."""
    rnd = game.rounds.get(round_name)
    if rnd is None:
        return {"categories": [], "cells": []}
    spent = set(used)
    cells = []
    for col in range(1, BOARD_COLS + 1):
        for row in range(1, BOARD_ROWS + 1):
            cid = f"{round_name}_{col}_{row}"
            clue = rnd.clues.get(cid)
            if clue is None:
                continue
            cells.append({
                "clue_id": cid,
                "col": col,
                "row": row,
                "value": clue.value,
                "available": cid not in spent,
            })
    return {"categories": list(rnd.categories), "cells": cells}


def assert_no_leak(payload: Any) -> None:
    """Recursively assert a payload carries no undisclosed secret. Used in tests
    and as a cheap runtime guard on the broadcast path."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in SECRET_KEYS or key in SEALED_KEYS:
                raise SpoilerError(f"payload leaked {key!r}")
            assert_no_leak(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_no_leak(item)
