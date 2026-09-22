"""SSE fan-out.

Broadcast only ever enqueues onto per-client bounded queues; it never writes to a
socket. One wedged or vanished client therefore cannot stall the game loop — it
just fills its queue and gets reaped.
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
from typing import Any, Callable, Iterator

QUEUE_MAX = 64
HEARTBEAT_S = 15

_HEARTBEAT_FRAME = b": heartbeat\n\n"


def encode(event_type: str, payload: dict[str, Any]) -> bytes:
    """One SSE frame. `data` is a single line because json.dumps never emits raw
    newlines, which keeps the frame grammar trivial on the client side."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {body}\n\n".encode("utf-8")


class Subscription:
    """Iterating yields encoded SSE frames, emitting a heartbeat comment on idle."""

    _ids = itertools.count(1)

    def __init__(self, player_id: str) -> None:
        self.id = next(Subscription._ids)
        self.player_id = player_id
        self.alive = True
        self.dropped = False      # set when the queue overflowed, for diagnostics
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_MAX)

    def offer(self, frame: bytes) -> bool:
        """Non-blocking enqueue. False means this subscriber is too far behind."""
        if not self.alive:
            return False
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            self.dropped = True
            self.alive = False
            return False
        return True

    def close(self) -> None:
        self.alive = False
        # A best-effort nudge so a stream blocked in get() returns before its
        # timeout; an already-full queue just waits out the heartbeat instead.
        try:
            self._q.put_nowait(b"")
        except queue.Full:
            pass

    def stream(self) -> Iterator[bytes]:
        while self.alive:
            try:
                frame = self._q.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                yield _HEARTBEAT_FRAME
                continue
            if not frame:
                break
            yield frame


class Hub:
    def __init__(self, on_reap: Callable[[str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Subscription] = {}
        self._on_reap = on_reap

    def subscribe(self, player_id: str) -> Subscription:
        sub = Subscription(player_id)
        with self._lock:
            self._subs[sub.id] = sub
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            self._subs.pop(sub.id, None)
        sub.close()

    def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        self._fan(encode(event_type, payload), None)

    def send_to(self, player_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._fan(encode(event_type, payload), player_id)

    def connected_ids(self) -> list[str]:
        with self._lock:
            return sorted({s.player_id for s in self._subs.values() if s.alive})

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def close_all(self) -> None:
        with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            sub.close()

    def _fan(self, frame: bytes, only: str | None) -> None:
        with self._lock:
            targets = [s for s in self._subs.values()
                       if only is None or s.player_id == only]
        reaped: list[Subscription] = []
        for sub in targets:
            if not sub.offer(frame):
                reaped.append(sub)
        if not reaped:
            return
        with self._lock:
            for sub in reaped:
                self._subs.pop(sub.id, None)
            survivors = {s.player_id for s in self._subs.values() if s.alive}
        if self._on_reap is None:
            return
        for pid in sorted({s.player_id for s in reaped} - survivors):
            self._on_reap(pid)
