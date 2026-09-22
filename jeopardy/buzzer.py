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

from .model import Buzz


def classify(press_server: int, rtt_ms: int, unlock_at: int, received_at: int
             ) -> tuple[str, int]:
    """Validate one press time. Returns (status, effective_press_server).

    early   press before unlock; ignored, and the player may press again
    future  press time ahead of receipt beyond FUTURE_TOLERANCE_MS
    clamped press backdated further than rtt + BACKDATE_MARGIN_MS explains;
            the effective time is raised to that floor and the host panel flags it
    """
    raise NotImplementedError


def resolve_order(buzzes: list[Buzz], clue_id: str) -> list[Buzz]:
    """Sort accepted buzzes by effective press time, fill in delta_ms relative to
    the winner, and break exact ties deterministically via sha256(player_id+clue_id)
    so the result never depends on arrival order."""
    raise NotImplementedError


def estimate_offset(samples: list[tuple[int, int, int]]) -> tuple[int, int]:
    """Given (t_send, t_server, t_recv) samples, return (offset_ms, rtt_ms) from
    the minimum-RTT sample, which carries the least queuing noise."""
    raise NotImplementedError
