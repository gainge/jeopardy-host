"""Clock-offset math and buzz-order reconciliation.

The whole point: buzz order is decided by when a player physically pressed, not by
whose packet arrived first. Clients translate their local press time into server
time using an offset measured by Cristian's algorithm; the server collects every
buzz landing inside a short window and sorts by that translated time.

Client-reported timestamps are forgeable. The clamps here make backdating
detectable and bounded rather than impossible — consistent with the honor system
the rest of the game already runs on.
"""

from __future__ import annotations

import hashlib

from . import protocol as P
from .model import (BUZZ_ACCEPTED, BUZZ_CLAMPED, BUZZ_EARLY, BUZZ_FUTURE, Buzz)

# Statuses that still take part in the race. A clamped press is suspicious, not
# void: it is ordered at the earliest time the network could actually explain.
RACING = (BUZZ_ACCEPTED, BUZZ_CLAMPED)


def classify(press_server: int, rtt_ms: int, unlock_at: int, received_at: int
             ) -> tuple[str, int]:
    """Validate one press time. Returns (status, effective_press_server).

    early   press before unlock; ignored, and the player may press again
    future  press time ahead of receipt beyond FUTURE_TOLERANCE_MS
    clamped press backdated further than rtt + BACKDATE_MARGIN_MS explains;
            the effective time is raised to that floor and the host panel flags it
    """
    if press_server > received_at + P.FUTURE_TOLERANCE_MS:
        return BUZZ_FUTURE, press_server
    if press_server < unlock_at:
        return BUZZ_EARLY, press_server
    floor = received_at - rtt_ms - P.BACKDATE_MARGIN_MS
    if press_server < floor:
        return BUZZ_CLAMPED, max(floor, unlock_at)
    return BUZZ_ACCEPTED, press_server


def _tiebreak(player_id: str, clue_id: str) -> str:
    return hashlib.sha256(f"{player_id}{clue_id}".encode("utf-8")).hexdigest()


def resolve_order(buzzes: list[Buzz], clue_id: str) -> list[Buzz]:
    """Sort accepted buzzes by effective press time, fill in delta_ms relative to
    the winner, and break exact ties deterministically via sha256(player_id+clue_id)
    so the result never depends on arrival order."""
    racing = [b for b in buzzes if b.status in RACING]
    racing.sort(key=lambda b: (b.press_server, _tiebreak(b.player_id, clue_id)))
    if racing:
        winner = racing[0].press_server
        for b in racing:
            b.delta_ms = b.press_server - winner
    return racing


def estimate_offset(samples: list[tuple[int, int, int]]) -> tuple[int, int]:
    """Given (t_send, t_server, t_recv) samples, return (offset_ms, rtt_ms) from
    the minimum-RTT sample, which carries the least queuing noise."""
    best: tuple[int, int] | None = None
    for t_send, t_server, t_recv in samples:
        rtt = t_recv - t_send
        # Cristian: assume the reply spent half the round trip in flight.
        offset = t_server + rtt // 2 - t_recv
        if best is None or rtt < best[1]:
            best = (offset, rtt)
    return best if best is not None else (0, 0)
