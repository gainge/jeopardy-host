"""SSE fan-out.

Broadcast only ever enqueues onto per-client bounded queues; it never writes to a
socket. One wedged or vanished client therefore cannot stall the game loop — it
just fills its queue and gets reaped.
"""

from __future__ import annotations

from typing import Any, Iterator


class Hub:
    def subscribe(self, player_id: str) -> "Subscription": ...
    def unsubscribe(self, sub: "Subscription") -> None: ...
    def broadcast(self, event_type: str, payload: dict[str, Any]) -> None: ...
    def send_to(self, player_id: str, event_type: str, payload: dict[str, Any]) -> None: ...
    def connected_ids(self) -> list[str]: ...


class Subscription:
    """Iterating yields encoded SSE frames, emitting a heartbeat comment on idle."""
    def stream(self) -> Iterator[bytes]: ...
