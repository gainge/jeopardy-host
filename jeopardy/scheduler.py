"""A single timer thread owning a deadline heap.

The engine is clockless; this is what wakes it. One thread, one condition
variable — deliberately not a sprawl of threading.Timer objects, so a rescheduled
deadline can never leave a stale timer racing the current state.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


def now_ms() -> int:
    """Server wall-clock milliseconds. The single time source clients calibrate to."""
    return int(time.time() * 1000)


class Scheduler:
    def __init__(self, on_due: Callable[[int], None]) -> None:
        self._on_due = on_due
        self._cv = threading.Condition()
        self._at: int | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._cv:
            if self._running:
                return
            self._running = True
            # Started while holding the lock so stop() can never observe a thread
            # object that has been created but not yet started.
            self._thread = threading.Thread(target=self._loop, name="scheduler",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._running = False
            thread, self._thread = self._thread, None
            self._cv.notify_all()
        if thread is not None:
            thread.join(timeout=2)

    def schedule(self, at_ms: int | None) -> None:
        """Request a wake-up at `at_ms`. Replaces any later pending wake."""
        with self._cv:
            if at_ms is None:
                self._at = None
            elif self._at is None or at_ms < self._at:
                self._at = at_ms
            else:
                return
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                if not self._running:
                    return
                if self._at is None:
                    self._cv.wait(timeout=1.0)
                    continue
                delay = (self._at - now_ms()) / 1000.0
                if delay > 0:
                    self._cv.wait(timeout=delay)
                    continue
                self._at = None
            # The callback re-enters schedule() with the engine's next deadline,
            # so it must not run while holding the condition.
            try:
                self._on_due(now_ms())
            except Exception:  # a rules bug must not kill the only timer thread
                import traceback
                traceback.print_exc()
