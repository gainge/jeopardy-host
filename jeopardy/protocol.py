"""The wire contract: every command a client may send and every event the server emits.

This module is the Phase 0 contract. The engine, the transport, and the browser
client are written against the names and payload shapes declared here, so it must
not import from any of them.

Validation is intentionally a tiny hand-rolled schema check rather than a library,
because the deployment target permits no third-party packages.
"""

from __future__ import annotations

from typing import Any, Callable

PROTOCOL_VERSION = 1

# --- Commands: client -> server (POST /api/command) --------------------------

CMD_JOIN = "join"
CMD_SET_CLOCK = "set_clock"          # report measured offset/rtt for the host panel
CMD_SELECT_CLUE = "select_clue"
CMD_BUZZ = "buzz"
CMD_SELF_REPORT = "self_report"      # buzz winner declares their own correctness
CMD_DD_WAGER = "dd_wager"
CMD_FINAL_WAGER = "final_wager"
CMD_FINAL_ANSWER = "final_answer"
CMD_FINAL_REPORT = "final_report"

# Host-only. None of these carry or return clue answers to the HOST — but
# CMD_HOST_REVEAL_RESPONSE does trigger disclosure to whoever currently holds the
# answer, at the host's discretion (see EVT_ANSWER_DISCLOSED below).
CMD_HOST_START_ROUND = "host_start_round"
CMD_HOST_NEXT_ROUND = "host_next_round"
CMD_HOST_FORCE_RESOLVE = "host_force_resolve"
CMD_HOST_REVEAL_RESPONSE = "host_reveal_response"
CMD_HOST_ADJUST_SCORE = "host_adjust_score"
CMD_HOST_SET_CONTROL = "host_set_control"
CMD_HOST_UNDO = "host_undo"
CMD_HOST_KICK = "host_kick"

HOST_COMMANDS = frozenset({
    CMD_HOST_START_ROUND, CMD_HOST_NEXT_ROUND, CMD_HOST_FORCE_RESOLVE,
    CMD_HOST_REVEAL_RESPONSE, CMD_HOST_ADJUST_SCORE, CMD_HOST_SET_CONTROL,
    CMD_HOST_UNDO, CMD_HOST_KICK,
})

# --- Events: server -> client (SSE on /events) -------------------------------

EVT_SNAPSHOT = "snapshot"            # targeted; full redacted state on connect/resync
EVT_ERROR = "error"                  # targeted
EVT_PLAYERS = "players"              # roster + scores changed
EVT_ROUND_STARTED = "round_started"
EVT_CLUE_SELECTED = "clue_selected"
EVT_BUZZERS_OPEN = "buzzers_open"
EVT_BUZZ_REGISTERED = "buzz_registered"   # someone buzzed; order still undecided
EVT_BUZZ_RESULT = "buzz_result"           # window closed; ordered race readout
EVT_ANSWERING = "answering"
EVT_ANSWER_REVEALED = "answer_revealed"   # broadcast fact only: {clue_id, player_id}
EVT_ANSWER_DISCLOSED = "answer_disclosed" # targeted at the answer holder; carries the answer
EVT_ANSWER_RESULT = "answer_result"
EVT_CLUE_RESOLVED = "clue_resolved"       # the only event that discloses the answer to everyone
EVT_DD_REVEALED = "dd_revealed"
EVT_DD_WAGER_SET = "dd_wager_set"
EVT_FINAL_CATEGORY = "final_category"
EVT_FINAL_WAGERS_OPEN = "final_wagers_open"
EVT_FINAL_WAGER_RECEIVED = "final_wager_received"   # who has submitted, not what
EVT_FINAL_CLUE = "final_clue"
EVT_FINAL_ANSWER_RECEIVED = "final_answer_received"
EVT_FINAL_REVEAL = "final_reveal"         # all wagers + answers + correct answer
EVT_FINAL_RESULT = "final_result"
EVT_SCORE_ADJUSTED = "score_adjusted"
EVT_GAME_OVER = "game_over"
EVT_CONTROL_CHANGED = "control_changed"

# --- Game phases and clue steps ---------------------------------------------

PHASE_LOBBY = "lobby"
PHASE_ROUND = "round"
PHASE_FINAL = "final"
PHASE_GAME_OVER = "game_over"

STEP_READING = "reading"          # clue displayed, buzzers locked, countdown running
STEP_OPEN = "open"                # buzzers live
STEP_WINDOW = "window"            # first buzz landed; collecting the rest
STEP_ANSWERING = "answering"      # a player holds the buzz and is speaking
STEP_DD_WAGER = "dd_wager"
STEP_DD_ANSWERING = "dd_answering"
STEP_RESOLVED = "resolved"

FINAL_CATEGORY = "category"
FINAL_WAGERS = "wagers"
FINAL_CLUE = "clue"
FINAL_ANSWERS = "answers"
FINAL_REVEAL = "reveal"
FINAL_DONE = "done"

# --- Timing defaults (milliseconds) -----------------------------------------

READ_MS_PER_CHAR = 1000 / 12.0
READ_MS_MIN = 4000
READ_MS_MAX = 10000
BUZZ_WINDOW_MS = 300        # reconciliation window opened by the first buzz
ANSWER_TIMEOUT_MS = 12000   # how long a buzz winner has before forfeiting
FUTURE_TOLERANCE_MS = 150   # slack for a press timestamp slightly ahead of receipt
BACKDATE_MARGIN_MS = 250    # slack beyond measured RTT before a press is clamped
DD_MIN_WAGER = 5
FINAL_ANSWER_MS = 45000


def read_ms_for(text: str) -> int:
    """Reading time before buzzers unlock, scaled to clue length and clamped."""
    return int(max(READ_MS_MIN, min(READ_MS_MAX, len(text) * READ_MS_PER_CHAR)))


# --- Minimal schema validation ----------------------------------------------

class ProtocolError(ValueError):
    """A command was malformed. Never raised for rule violations — only shape."""


def _str(max_len: int = 200) -> Callable[[Any], Any]:
    def check(v: Any) -> str:
        if not isinstance(v, str):
            raise ProtocolError("expected a string")
        v = v.strip()
        if not v or len(v) > max_len:
            raise ProtocolError(f"string must be 1..{max_len} characters")
        return v
    return check


def _int(lo: int | None = None, hi: int | None = None) -> Callable[[Any], Any]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ProtocolError("expected an integer")
        if lo is not None and v < lo:
            raise ProtocolError(f"must be >= {lo}")
        if hi is not None and v > hi:
            raise ProtocolError(f"must be <= {hi}")
        return v
    return check


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ProtocolError("expected a boolean")
    return v


_MS = _int(0, 2 ** 53)

# field name -> validator, per command. Absent fields are rejected; extra fields
# are dropped, so a client cannot smuggle state through an unvalidated key.
COMMAND_SCHEMA: dict[str, dict[str, Callable[[Any], Any]]] = {
    CMD_JOIN: {"name": _str(24)},
    CMD_SET_CLOCK: {"offset_ms": _int(-2**31, 2**31), "rtt_ms": _int(0, 60000)},
    CMD_SELECT_CLUE: {"clue_id": _str(16)},
    CMD_BUZZ: {
        "clue_id": _str(16),
        "press_server": _MS,
        "offset_ms": _int(-2**31, 2**31),
        "rtt_ms": _int(0, 60000),
    },
    CMD_SELF_REPORT: {"clue_id": _str(16), "correct": _bool},
    CMD_DD_WAGER: {"amount": _int(0, 10**7)},
    CMD_FINAL_WAGER: {"amount": _int(0, 10**7)},
    CMD_FINAL_ANSWER: {"text": _str(200)},
    CMD_FINAL_REPORT: {"correct": _bool},
    CMD_HOST_START_ROUND: {},
    CMD_HOST_NEXT_ROUND: {},
    CMD_HOST_FORCE_RESOLVE: {},
    CMD_HOST_REVEAL_RESPONSE: {},
    CMD_HOST_ADJUST_SCORE: {"player_id": _str(64), "delta": _int(-10**7, 10**7),
                            "reason": _str(120)},
    CMD_HOST_SET_CONTROL: {"player_id": _str(64)},
    CMD_HOST_UNDO: {},
    CMD_HOST_KICK: {"player_id": _str(64)},
}


def validate_command(payload: Any) -> tuple[str, dict[str, Any]]:
    """Shape-check a decoded command body. Returns (command_type, clean_fields)."""
    if not isinstance(payload, dict):
        raise ProtocolError("command must be a JSON object")
    ctype = payload.get("type")
    if not isinstance(ctype, str) or ctype not in COMMAND_SCHEMA:
        raise ProtocolError(f"unknown command type: {ctype!r}")
    schema = COMMAND_SCHEMA[ctype]
    clean: dict[str, Any] = {}
    for key, check in schema.items():
        if key not in payload:
            raise ProtocolError(f"{ctype}: missing field {key!r}")
        try:
            clean[key] = check(payload[key])
        except ProtocolError as exc:
            raise ProtocolError(f"{ctype}.{key}: {exc}") from None
    return ctype, clean
