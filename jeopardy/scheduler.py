"""A single timer thread owning a deadline heap.

The engine is clockless; this is what wakes it. One thread, one condition
variable — deliberately not a sprawl of threading.Timer objects, so a rescheduled
deadline can never leave a stale timer racing the current state.
"""

from __future__ import annotations

from typing import Callable


class Scheduler:
    def __init__(self, on_due: Callable[[int], None]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def schedule(self, at_ms: int) -> None:
        """Request a wake-up at `at_ms`. Replaces any later pending wake."""
