"""Append-only command journal.

Because the engine is deterministic, replaying the journal reproduces state
exactly — which is what gives crash recovery and host undo for free. We journal
*commands* (plus clock ticks) rather than events, so there is exactly one reducer
in the system and no chance of it drifting from a second one.

Host undo appends a compensating entry rather than truncating, so the audit trail
survives and the host can always explain a score.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

TICK = "__tick__"


class JournalError(RuntimeError):
    """The journal on disk is unreadable. Refuse to resume rather than resume wrong."""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")
        self._closed = False

    def append(self, actor_id: str | None, ctype: str, fields: dict[str, Any],
               now_ms: int) -> None:
        entry = {"actor": actor_id, "type": ctype, "fields": fields,
                 "now_ms": now_ms}
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True)
        with self._lock:
            if self._closed:
                return
            self._fh.write(line + "\n")
            self._fh.flush()
            # A handful of writes per minute buys durability across a hard kill.
            os.fsync(self._fh.fileno())

    def entries(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    # A crash mid-write can only ever truncate the final line.
                    if _is_last_line(self.path, lineno):
                        break
                    raise JournalError(f"{self.path}:{lineno}: {exc}") from None
                out.append(entry)
        return out

    def replay(self, engine: Any) -> None:
        """Re-apply the journal to a fresh engine, restoring exact state."""
        for entry in self.entries():
            now = entry["now_ms"]
            ctype = entry["type"]
            if ctype == TICK:
                engine.tick(now)
            elif not ctype.startswith("__"):
                # Other dunder types are transport bookkeeping (tokens), not
                # commands, and the engine must never see them.
                engine.apply(entry["actor"], ctype, entry["fields"], now)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()


def _is_last_line(path: str, lineno: int) -> bool:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh) == lineno
