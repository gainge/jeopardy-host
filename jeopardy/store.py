"""Append-only command journal.

Because the engine is deterministic, replaying the journal reproduces state
exactly — which is what gives crash recovery and host undo for free. We journal
*commands* (plus clock ticks) rather than events, so there is exactly one reducer
in the system and no chance of it drifting from a second one.

Host undo appends a compensating entry rather than truncating, so the audit trail
survives and the host can always explain a score.
"""

from __future__ import annotations

from typing import Any

TICK = "__tick__"


class Store:
    def __init__(self, path: str) -> None: ...

    def append(self, actor_id: str | None, ctype: str, fields: dict[str, Any],
               now_ms: int) -> None: ...

    def replay(self, engine: Any) -> None:
        """Re-apply the journal to a fresh engine, restoring exact state."""

    def close(self) -> None: ...
