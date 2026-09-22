"""Authoritative game rules as a pure state machine.

PURITY CONTRACT — enforced by tests/test_engine.py:
  * no imports of time, socket, http, threading, or random
  * no file or network I/O
  * every time-dependent decision takes `now_ms` as an argument

The transport owns the clock and calls `tick()` when `next_deadline()` comes due.
Because the engine is deterministic, the command journal in store.py can replay a
game exactly, which is what makes crash recovery and host undo work.

Two structural decisions carry most of the weight here:

Deadlines are drained at the top of `apply()` before the command is handled, so a
command processed at T always observes every transition that was due at or before
T. Replay of the same journal therefore produces the same events regardless of how
the wall clock happened to interleave with arrivals.

Clue resolution is itself a deadline (`ClueState.resolve_at`), never an inline side
effect of a command. That gives the one event that discloses an answer a single
code path, so the spoiler boundary is auditable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import buzzer
from . import protocol as P
from . import redact
from .model import (BUZZ_DUPLICATE, ROUND_DJ, ROUND_FJ, ROUND_J,
                    Buzz, Game, Player)


@dataclass
class Event:
    """One thing that happened. `targets=None` means broadcast to everyone."""
    type: str
    payload: dict[str, Any]
    targets: list[str] | None = None


@dataclass
class ClueState:
    clue_id: str
    step: str
    selected_by: str | None = None
    unlock_at: int | None = None
    window_closes_at: int | None = None
    answer_deadline: int | None = None
    resolve_at: int | None = None
    buzzes: list[Buzz] = field(default_factory=list)
    order: list[str] = field(default_factory=list)   # resolved buzz order
    order_index: int = 0
    attempted: list[str] = field(default_factory=list)
    is_daily_double: bool = False
    dd_player: str | None = None
    dd_wager: int | None = None
    revealed_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["buzzes"] = [b.to_dict() for b in self.buzzes]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClueState":
        d = dict(d)
        d["buzzes"] = [Buzz.from_dict(b) for b in d.get("buzzes", [])]
        return cls(**d)


@dataclass
class FinalState:
    step: str
    wagers: dict[str, int] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)
    verdicts: dict[str, bool] = field(default_factory=dict)
    deadline: int | None = None
    participants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FinalState":
        return cls(**d)


@dataclass
class ScoreEntry:
    """One reversible money movement. host_undo appends the inverse of the last
    entry rather than rewriting it, so the journal stays append-only."""
    player_id: str
    delta: int
    reason: str


@dataclass
class GameState:
    phase: str
    round: str | None = None
    players: dict[str, Player] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)      # stable seating order
    used: list[str] = field(default_factory=list)       # spent clue ids
    control: str | None = None
    clue: ClueState | None = None
    final: FinalState | None = None
    ledger: list[ScoreEntry] = field(default_factory=list)
    joined: int = 0                                     # monotonic id source

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "round": self.round,
            "players": {k: v.to_dict() for k, v in self.players.items()},
            "order": list(self.order),
            "used": list(self.used),
            "control": self.control,
            "clue": self.clue.to_dict() if self.clue else None,
            "final": self.final.to_dict() if self.final else None,
            "ledger": [e.__dict__ for e in self.ledger],
            "joined": self.joined,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GameState":
        return cls(
            phase=d["phase"],
            round=d.get("round"),
            players={k: Player.from_dict(v) for k, v in d.get("players", {}).items()},
            order=list(d.get("order", [])),
            used=list(d.get("used", [])),
            control=d.get("control"),
            clue=ClueState.from_dict(d["clue"]) if d.get("clue") else None,
            final=FinalState.from_dict(d["final"]) if d.get("final") else None,
            ledger=[ScoreEntry(**e) for e in d.get("ledger", [])],
            joined=d.get("joined", 0),
        )


class Engine:
    """Applies commands to state and returns the events that resulted."""

    def __init__(self, game: Game, state: GameState | None = None) -> None:
        self.game = game
        self.state = state or GameState(phase=P.PHASE_LOBBY)

    # --- entry points -------------------------------------------------------

    def apply(self, actor_id: str | None, ctype: str, fields: dict[str, Any],
              now_ms: int) -> list[Event]:
        """Apply one validated command. Rule violations return an EVT_ERROR event
        targeted at the actor rather than raising."""
        events = self._pump(now_ms)
        if ctype == P.CMD_JOIN:
            return events + self._join(fields["name"], now_ms)
        actor = self.state.players.get(actor_id or "")
        if actor is None:
            return events + [self._error(actor_id, "you are not in this game")]
        if ctype in P.HOST_COMMANDS and not actor.is_host:
            return events + [self._error(actor_id, "host only")]
        handler = self._HANDLERS.get(ctype)
        if handler is None:
            return events + [self._error(actor_id, f"unhandled command {ctype}")]
        return events + handler(self, actor, fields, now_ms)

    def tick(self, now_ms: int) -> list[Event]:
        """Fire any deadline that has come due (unlock, buzz window, timeout)."""
        return self._pump(now_ms)

    def next_deadline(self) -> int | None:
        """Earliest pending deadline in server ms, or None if nothing is scheduled."""
        clue = self.state.clue
        if clue is not None:
            if clue.resolve_at is not None:
                return clue.resolve_at
            if clue.step == P.STEP_READING:
                return clue.unlock_at
            if clue.step == P.STEP_WINDOW:
                return clue.window_closes_at
            if clue.step in (P.STEP_ANSWERING, P.STEP_DD_ANSWERING):
                return clue.answer_deadline
            return None
        final = self.state.final
        if final is not None:
            return final.deadline
        return None

    def snapshot_for(self, player_id: str) -> dict[str, Any]:
        """Full redacted state for one recipient. Must route through redact.py."""
        st = self.state
        me = st.players.get(player_id)
        board = None
        if st.round in (ROUND_J, ROUND_DJ):
            board = redact.public_board(self.game, st.round, st.used)
        snap = {
            "phase": st.phase,
            "round": st.round,
            "you": player_id,
            "is_host": bool(me and me.is_host),
            "players": self._roster(),
            "control": st.control,
            "board": board,
            "clue": self._public_clue_state(player_id),
            "final": self._public_final_state(player_id),
        }
        redact.assert_no_leak(snap)
        return snap

    # --- roster -------------------------------------------------------------

    def _roster(self) -> list[dict[str, Any]]:
        return [self.state.players[p].to_dict() for p in self.state.order
                if p in self.state.players]

    def _top_value(self, round_name: str) -> int:
        """The round's largest board value, read from the loaded game rather than a
        constant: pre-2001 shows used a $100-$500 board, so a hardcoded ceiling
        would misprice their Daily Double wagers."""
        clues = self.game.rounds[round_name].clues.values()
        return max((c.value for c in clues), default=0)

    def _scores(self) -> dict[str, int]:
        return {p: self.state.players[p].score for p in self.state.order
                if p in self.state.players}

    def _error(self, target: str | None, message: str) -> Event:
        return Event(P.EVT_ERROR, {"message": message},
                     targets=[target] if target else [])

    def _players_event(self, **extra: Any) -> Event:
        payload: dict[str, Any] = {"players": self._roster()}
        payload.update(extra)
        return Event(P.EVT_PLAYERS, payload)

    def _join(self, name: str, now_ms: int) -> list[Event]:
        st = self.state
        st.joined += 1
        # Deterministic ids: store.py replays the journal and must land on the
        # same identifiers, so nothing here may consult a random source.
        pid = f"p{st.joined}"
        is_host = not st.players
        st.players[pid] = Player(id=pid, name=name, is_host=is_host)
        st.order.append(pid)
        if is_host or st.control is None:
            st.control = st.control or pid
        return [self._players_event(player_id=pid)]

    # --- scoring ------------------------------------------------------------

    def _award(self, pid: str, delta: int, reason: str) -> None:
        player = self.state.players.get(pid)
        if player is None:
            return
        player.score += delta
        self.state.ledger.append(ScoreEntry(pid, delta, reason))

    # --- round control ------------------------------------------------------

    def _start_round(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        if st.phase != P.PHASE_LOBBY:
            return [self._error(actor.id, "the game has already started")]
        return self._enter_round(ROUND_J)

    def _enter_round(self, name: str) -> list[Event]:
        st = self.state
        st.phase = P.PHASE_ROUND
        st.round = name
        st.clue = None
        st.used = [c for c in st.used if not c.startswith(f"{name}_")]
        board = redact.public_board(self.game, name, st.used)
        if st.control not in st.players:
            st.control = st.order[0] if st.order else None
        return [
            Event(P.EVT_ROUND_STARTED, {"round": name, **board}),
            Event(P.EVT_CONTROL_CHANGED, {"player_id": st.control}),
            self._players_event(),
        ]

    def _next_round(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        if st.phase == P.PHASE_LOBBY:
            return self._enter_round(ROUND_J)
        if st.phase == P.PHASE_ROUND and st.round == ROUND_J:
            return self._enter_round(ROUND_DJ)
        if st.phase == P.PHASE_ROUND:
            return self._enter_final(now)
        if st.phase == P.PHASE_FINAL:
            return self._advance_final(now)
        return [self._error(actor.id, "the game is over")]

    # --- clue selection -----------------------------------------------------

    def _select_clue(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        cid = fields["clue_id"]
        if st.phase != P.PHASE_ROUND:
            return [self._error(actor.id, "no board is in play")]
        if st.clue is not None:
            return [self._error(actor.id, "a clue is already in play")]
        if st.control != actor.id:
            return [self._error(actor.id, "you do not have control of the board")]
        if cid in st.used:
            return [self._error(actor.id, "that cell has been played")]
        try:
            clue = self.game.clue(cid)
        except KeyError:
            return [self._error(actor.id, "no such clue")]
        if clue.round != st.round:
            return [self._error(actor.id, "that clue is not on this board")]

        public = redact.public_clue(self.game, cid)
        if clue.is_daily_double:
            st.clue = ClueState(clue_id=cid, step=P.STEP_DD_WAGER,
                                selected_by=actor.id, is_daily_double=True,
                                dd_player=actor.id)
            top = self._top_value(st.round)
            return [
                Event(P.EVT_CLUE_SELECTED,
                      {**public, "unlock_at": None, "selected_by": actor.id}),
                # Everyone learns it is a Daily Double at the same instant; that
                # simultaneity is what lets the host play unspoiled.
                Event(P.EVT_DD_REVEALED, {
                    "clue_id": cid, "player_id": actor.id,
                    "min_wager": P.DD_MIN_WAGER,
                    "max_wager": max(actor.score, top),
                }),
            ]

        unlock_at = now + P.read_ms_for(clue.text)
        st.clue = ClueState(clue_id=cid, step=P.STEP_READING,
                            selected_by=actor.id, unlock_at=unlock_at)
        return [Event(P.EVT_CLUE_SELECTED,
                      {**public, "unlock_at": unlock_at, "selected_by": actor.id})]

    # --- daily double -------------------------------------------------------

    def _dd_wager(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        clue = st.clue
        if clue is None or clue.step != P.STEP_DD_WAGER:
            return [self._error(actor.id, "no Daily Double wager is open")]
        if clue.dd_player != actor.id:
            return [self._error(actor.id, "the Daily Double is not yours")]
        top = self._top_value(st.round)
        lo, hi = P.DD_MIN_WAGER, max(actor.score, top)
        amount = fields["amount"]
        if not lo <= amount <= hi:
            return [self._error(actor.id, f"wager must be {lo}..{hi}")]
        clue.dd_wager = amount
        clue.step = P.STEP_DD_ANSWERING
        clue.answer_deadline = now + P.ANSWER_TIMEOUT_MS
        return [
            Event(P.EVT_DD_WAGER_SET,
                  {"clue_id": clue.clue_id, "player_id": actor.id, "amount": amount}),
            Event(P.EVT_ANSWERING, {"clue_id": clue.clue_id, "player_id": actor.id,
                                    "deadline": clue.answer_deadline}),
        ]

    # --- buzzing ------------------------------------------------------------

    def _buzz(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        clue = st.clue
        if clue is None or clue.clue_id != fields["clue_id"]:
            return [self._error(actor.id, "that clue is not in play")]
        if clue.is_daily_double:
            return [self._error(actor.id, "Daily Doubles are not buzzed")]
        if clue.step not in (P.STEP_READING, P.STEP_OPEN, P.STEP_WINDOW):
            return [self._error(actor.id, "the buzzers are closed")]

        already = {b.player_id for b in clue.buzzes
                   if b.status in buzzer.RACING} | set(clue.attempted)
        if actor.id in already:
            status, press = BUZZ_DUPLICATE, fields["press_server"]
        else:
            status, press = buzzer.classify(
                fields["press_server"], fields["rtt_ms"],
                clue.unlock_at or 0, now)

        clue.buzzes.append(Buzz(
            player_id=actor.id, press_server=press, offset_ms=fields["offset_ms"],
            rtt_ms=fields["rtt_ms"], received_at=now, status=status))

        if status not in buzzer.RACING:
            # Recorded, not dropped: buzz_result will show the host panel why.
            return [self._error(actor.id, f"buzz {status}")]

        events = [Event(P.EVT_BUZZ_REGISTERED,
                        {"clue_id": clue.clue_id, "player_id": actor.id})]
        if clue.step == P.STEP_OPEN:
            # The first press opens a reconciliation window; ordering waits for it
            # so that a slower connection cannot cost a faster thumb.
            clue.step = P.STEP_WINDOW
            clue.window_closes_at = now + P.BUZZ_WINDOW_MS
        return events

    # --- self reporting -----------------------------------------------------

    def _self_report(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        clue = st.clue
        if clue is None or clue.clue_id != fields["clue_id"]:
            return [self._error(actor.id, "that clue is not in play")]
        if clue.step not in (P.STEP_ANSWERING, P.STEP_DD_ANSWERING):
            return [self._error(actor.id, "nobody is answering")]
        holder = clue.dd_player if clue.is_daily_double else clue.order[clue.order_index]
        if actor.id != holder:
            return [self._error(actor.id, "it is not your answer to report")]
        value = clue.dd_wager if clue.is_daily_double else self.game.clue(
            clue.clue_id).value
        correct = fields["correct"]
        delta = value if correct else -value
        self._award(actor.id, delta, f"{clue.clue_id} self-report")
        events = [Event(P.EVT_ANSWER_RESULT, {
            "clue_id": clue.clue_id, "player_id": actor.id,
            "correct": correct, "score_delta": delta})]
        if correct:
            st.control = actor.id
            events.append(Event(P.EVT_CONTROL_CHANGED, {"player_id": actor.id}))
            return events + self._schedule_resolution(now)
        return events + self._advance_after_miss(now)

    def _reveal_answer(self, actor: Player, fields: dict, now: int) -> list[Event]:
        """Host-triggered: let the current answer holder see the answer, privately,
        before they self-report. The host still never sees it — only the person
        who is about to be judged on it does."""
        clue = self.state.clue
        if clue is None or clue.step not in (P.STEP_ANSWERING, P.STEP_DD_ANSWERING):
            return [self._error(actor.id, "nobody is answering")]
        holder = clue.dd_player if clue.is_daily_double else clue.order[clue.order_index]
        if clue.revealed_to == holder:
            return [self._error(actor.id, "the answer is already revealed")]
        clue.revealed_to = holder
        # Revealing the answer stands in for "an answer was already given," so the
        # forfeiture clock no longer serves a purpose once the host confirms that.
        clue.answer_deadline = None
        answer = self.game.clue(clue.clue_id).answer
        return [
            Event(P.EVT_ANSWER_REVEALED, {"clue_id": clue.clue_id, "player_id": holder}),
            Event(P.EVT_ANSWER_DISCLOSED, {"clue_id": clue.clue_id, "answer": answer},
                  targets=[holder]),
        ]

    def _answer_timeout(self, at: int) -> list[Event]:
        """A held buzz that never produced an answer costs nothing but the turn."""
        clue = self.state.clue
        assert clue is not None
        holder = clue.dd_player if clue.is_daily_double else clue.order[clue.order_index]
        events = [Event(P.EVT_ANSWER_RESULT, {
            "clue_id": clue.clue_id, "player_id": holder,
            "correct": False, "score_delta": 0})]
        if clue.is_daily_double:
            return events + self._schedule_resolution(at)
        return events + self._advance_after_miss(at)

    def _advance_after_miss(self, now: int) -> list[Event]:
        """Hand the clue to the next finisher, or reopen it to whoever is left."""
        clue = self.state.clue
        assert clue is not None
        holder = clue.order[clue.order_index]
        if holder not in clue.attempted:
            clue.attempted.append(holder)
        clue.answer_deadline = None
        clue.revealed_to = None

        while clue.order_index + 1 < len(clue.order):
            clue.order_index += 1
            nxt = clue.order[clue.order_index]
            if nxt in clue.attempted:
                continue
            clue.step = P.STEP_ANSWERING
            clue.answer_deadline = now + P.ANSWER_TIMEOUT_MS
            return [Event(P.EVT_ANSWERING, {"clue_id": clue.clue_id,
                                            "player_id": nxt,
                                            "deadline": clue.answer_deadline})]

        remaining = [p for p in self.state.order
                     if p in self.state.players and p not in clue.attempted]
        if not remaining:
            return self._schedule_resolution(now)
        clue.buzzes = [b for b in clue.buzzes if b.player_id in clue.attempted]
        clue.order = []
        clue.order_index = 0
        clue.step = P.STEP_OPEN
        clue.window_closes_at = None
        return [Event(P.EVT_BUZZERS_OPEN, {"clue_id": clue.clue_id})]

    # --- deadline pump ------------------------------------------------------

    def _pump(self, now_ms: int) -> list[Event]:
        events: list[Event] = []
        # Bounded because each firing either consumes a player or ends the clue.
        for _ in range(256):
            due = self.next_deadline()
            if due is None or due > now_ms:
                break
            events.extend(self._fire(due))
        return events

    def _fire(self, at: int) -> list[Event]:
        clue = self.state.clue
        if clue is not None:
            if clue.resolve_at is not None:
                return self._resolve(at)
            if clue.step == P.STEP_READING:
                clue.step = P.STEP_OPEN
                return [Event(P.EVT_BUZZERS_OPEN, {"clue_id": clue.clue_id})]
            if clue.step == P.STEP_WINDOW:
                return self._close_window(at)
            if clue.step in (P.STEP_ANSWERING, P.STEP_DD_ANSWERING):
                return self._answer_timeout(at)
            return []
        if self.state.final is not None:
            return self._final_deadline(at)
        return []

    def _close_window(self, at: int) -> list[Event]:
        clue = self.state.clue
        assert clue is not None
        clue.window_closes_at = None
        ordered = buzzer.resolve_order(clue.buzzes, clue.clue_id)
        ordered = [b for b in ordered if b.player_id not in clue.attempted]
        clue.order = [b.player_id for b in ordered]
        clue.order_index = 0
        events = [Event(P.EVT_BUZZ_RESULT, {
            "clue_id": clue.clue_id,
            "order": [{"player_id": b.player_id, "delta_ms": b.delta_ms,
                       "status": b.status, "press_server": b.press_server}
                      for b in ordered],
            "rejected": [{"player_id": b.player_id, "status": b.status}
                         for b in clue.buzzes if b.status not in buzzer.RACING],
        })]
        if not clue.order:
            return events + self._schedule_resolution(at)
        clue.step = P.STEP_ANSWERING
        clue.answer_deadline = at + P.ANSWER_TIMEOUT_MS
        events.append(Event(P.EVT_ANSWERING, {
            "clue_id": clue.clue_id, "player_id": clue.order[0],
            "deadline": clue.answer_deadline}))
        return events

    def _schedule_resolution(self, at: int) -> list[Event]:
        clue = self.state.clue
        assert clue is not None
        clue.step = P.STEP_RESOLVED
        clue.answer_deadline = None
        clue.window_closes_at = None
        clue.resolve_at = at
        return []

    def _resolve(self, at: int) -> list[Event]:
        st = self.state
        clue = st.clue
        assert clue is not None
        st.clue = None
        if clue.clue_id not in st.used:
            st.used.append(clue.clue_id)
        answer = self.game.clue(clue.clue_id).answer
        return [Event(P.EVT_CLUE_RESOLVED, {
            "clue_id": clue.clue_id, "answer": answer, "scores": self._scores()})]

    # --- host powers --------------------------------------------------------

    def _force_resolve(self, actor: Player, fields: dict, now: int) -> list[Event]:
        if self.state.clue is None:
            return [self._error(actor.id, "no clue is in play")]
        # Resolution is a scheduled transition, so the answer is disclosed by the
        # same code path here as everywhere else.
        return self._schedule_resolution(now)

    def _adjust_score(self, actor: Player, fields: dict, now: int) -> list[Event]:
        pid = fields["player_id"]
        if pid not in self.state.players:
            return [self._error(actor.id, "no such player")]
        self._award(pid, fields["delta"], fields["reason"])
        return [Event(P.EVT_SCORE_ADJUSTED, {
            "player_id": pid, "delta": fields["delta"], "reason": fields["reason"],
            "by": actor.id, "score": self.state.players[pid].score})]

    def _undo(self, actor: Player, fields: dict, now: int) -> list[Event]:
        if not self.state.ledger:
            return [self._error(actor.id, "nothing to undo")]
        last = self.state.ledger.pop()
        player = self.state.players.get(last.player_id)
        if player is None:
            return [self._error(actor.id, "that player has left")]
        player.score -= last.delta
        return [Event(P.EVT_SCORE_ADJUSTED, {
            "player_id": last.player_id, "delta": -last.delta,
            "reason": f"undo: {last.reason}", "by": actor.id,
            "score": player.score})]

    def _set_control(self, actor: Player, fields: dict, now: int) -> list[Event]:
        pid = fields["player_id"]
        if pid not in self.state.players:
            return [self._error(actor.id, "no such player")]
        self.state.control = pid
        return [Event(P.EVT_CONTROL_CHANGED, {"player_id": pid})]

    def _kick(self, actor: Player, fields: dict, now: int) -> list[Event]:
        st = self.state
        pid = fields["player_id"]
        if pid not in st.players:
            return [self._error(actor.id, "no such player")]
        del st.players[pid]
        st.order = [p for p in st.order if p != pid]
        if st.control == pid:
            st.control = st.order[0] if st.order else None
        return [self._players_event(),
                Event(P.EVT_CONTROL_CHANGED, {"player_id": st.control})]

    def _set_clock(self, actor: Player, fields: dict, now: int) -> list[Event]:
        actor.offset_ms = fields["offset_ms"]
        actor.rtt_ms = fields["rtt_ms"]
        return [self._players_event()]

    # --- final jeopardy -----------------------------------------------------

    def _participants(self) -> list[str]:
        return [p for p in self.state.order
                if p in self.state.players and self.state.players[p].score > 0]

    def _enter_final(self, now: int) -> list[Event]:
        st = self.state
        st.phase = P.PHASE_FINAL
        st.round = ROUND_FJ
        st.clue = None
        st.final = FinalState(step=P.FINAL_CATEGORY, participants=self._participants())
        return [Event(P.EVT_FINAL_CATEGORY,
                      {"category": self.game.clue(ROUND_FJ).category})]

    def _advance_final(self, now: int) -> list[Event]:
        st = self.state
        fin = st.final
        assert fin is not None
        if fin.step == P.FINAL_CATEGORY:
            fin.step = P.FINAL_WAGERS
            fin.participants = self._participants()
            fin.deadline = now + P.FINAL_ANSWER_MS
            return [Event(P.EVT_FINAL_WAGERS_OPEN,
                          {"participants": list(fin.participants),
                           "deadline": fin.deadline})]
        if fin.step == P.FINAL_WAGERS:
            return self._open_final_clue(now)
        if fin.step == P.FINAL_CLUE or fin.step == P.FINAL_ANSWERS:
            return self._reveal_final(now)
        if fin.step == P.FINAL_REVEAL:
            fin.step = P.FINAL_DONE
            return self._end_game()
        return self._end_game()

    def _open_final_clue(self, now: int) -> list[Event]:
        fin = self.state.final
        assert fin is not None
        clue = self.game.clue(ROUND_FJ)
        fin.step = P.FINAL_ANSWERS
        fin.deadline = now + P.FINAL_ANSWER_MS
        return [Event(P.EVT_FINAL_CLUE, {"text": clue.text,
                                         "media": list(clue.media),
                                         "deadline": fin.deadline})]

    def _final_wager(self, actor: Player, fields: dict, now: int) -> list[Event]:
        fin = self.state.final
        if fin is None or fin.step != P.FINAL_WAGERS:
            return [self._error(actor.id, "wagering is not open")]
        if actor.id not in fin.participants:
            return [self._error(actor.id, "you are not playing Final Jeopardy")]
        amount = fields["amount"]
        if not 0 <= amount <= actor.score:
            return [self._error(actor.id, f"wager must be 0..{actor.score}")]
        fin.wagers[actor.id] = amount
        events = [Event(P.EVT_FINAL_WAGER_RECEIVED, {"player_id": actor.id})]
        if all(p in fin.wagers for p in fin.participants):
            events.extend(self._open_final_clue(now))
        return events

    def _final_answer(self, actor: Player, fields: dict, now: int) -> list[Event]:
        fin = self.state.final
        if fin is None or fin.step != P.FINAL_ANSWERS:
            return [self._error(actor.id, "answers are not open")]
        if actor.id not in fin.participants:
            return [self._error(actor.id, "you are not playing Final Jeopardy")]
        fin.answers[actor.id] = fields["text"]
        events = [Event(P.EVT_FINAL_ANSWER_RECEIVED, {"player_id": actor.id})]
        if all(p in fin.answers for p in fin.participants):
            events.extend(self._reveal_final(now))
        return events

    def _reveal_final(self, now: int) -> list[Event]:
        fin = self.state.final
        assert fin is not None
        fin.step = P.FINAL_REVEAL
        fin.deadline = None
        clue = self.game.clue(ROUND_FJ)
        # Reveal ascending by score, the way the show walks the podiums.
        alive = [p for p in fin.participants if p in self.state.players]
        ordered = sorted(alive, key=lambda p: (self.state.players[p].score, p))
        return [Event(P.EVT_FINAL_REVEAL, {
            "answer": clue.answer,
            "entries": [{"player_id": p,
                         "wager": fin.wagers.get(p, 0),
                         "answer": fin.answers.get(p, "")} for p in ordered],
        })]

    def _final_report(self, actor: Player, fields: dict, now: int) -> list[Event]:
        fin = self.state.final
        if fin is None or fin.step != P.FINAL_REVEAL:
            return [self._error(actor.id, "Final Jeopardy is not being scored")]
        if actor.id not in fin.participants:
            return [self._error(actor.id, "you are not playing Final Jeopardy")]
        if actor.id in fin.verdicts:
            return [self._error(actor.id, "you already reported")]
        correct = fields["correct"]
        wager = fin.wagers.get(actor.id, 0)
        delta = wager if correct else -wager
        fin.verdicts[actor.id] = correct
        self._award(actor.id, delta, "final self-report")
        events = [Event(P.EVT_FINAL_RESULT, {
            "player_id": actor.id, "correct": correct, "score_delta": delta,
            "scores": self._scores()})]
        if all(p in fin.verdicts for p in fin.participants):
            fin.step = P.FINAL_DONE
            events.extend(self._end_game())
        return events

    def _final_deadline(self, at: int) -> list[Event]:
        fin = self.state.final
        assert fin is not None
        if fin.step == P.FINAL_WAGERS:
            # A silent player wagers nothing rather than stalling the room.
            return self._open_final_clue(at)
        if fin.step == P.FINAL_ANSWERS:
            return self._reveal_final(at)
        fin.deadline = None
        return []

    def _end_game(self) -> list[Event]:
        st = self.state
        st.phase = P.PHASE_GAME_OVER
        scores = self._scores()
        winner = max(scores, key=lambda p: (scores[p], p)) if scores else None
        return [Event(P.EVT_GAME_OVER, {"scores": scores, "winner": winner})]

    # --- snapshot helpers ---------------------------------------------------

    def _public_clue_state(self, player_id: str) -> dict[str, Any] | None:
        clue = self.state.clue
        if clue is None or clue.step == P.STEP_RESOLVED:
            return None
        answering = None
        if clue.step == P.STEP_DD_ANSWERING:
            answering = clue.dd_player
        elif clue.step == P.STEP_ANSWERING and clue.order:
            answering = clue.order[clue.order_index]
        public = redact.public_clue(self.game, clue.clue_id)
        # revealed_player_id is a fact everyone may see (mirrors EVT_ANSWER_REVEALED);
        # your_revealed_answer is the earned disclosure itself, visible only to the
        # player it was revealed to — the same shape as Final Jeopardy's
        # your_wager/your_answer fields below.
        return {**public, "unlock_at": clue.unlock_at,
                "selected_by": clue.selected_by, "step": clue.step,
                "answering": answering, "revealed_player_id": clue.revealed_to,
                "your_revealed_answer": (self.game.clue(clue.clue_id).answer
                                         if clue.revealed_to == player_id else None)}

    def _public_final_state(self, player_id: str) -> dict[str, Any] | None:
        fin = self.state.final
        if fin is None:
            return None
        submitted = (list(fin.wagers) if fin.step == P.FINAL_WAGERS
                     else list(fin.answers))
        # A player reconnecting mid-Final has missed final_wagers_open and
        # final_clue, so the snapshot has to carry everything their form needs:
        # whether they are playing, and what they already committed to. Other
        # players' wagers and answers stay sealed until final_reveal.
        return {
            "step": fin.step,
            "category": self.game.clue(ROUND_FJ).category,
            "participants": list(fin.participants),
            "you_participate": player_id in fin.participants,
            "you_wagered": player_id in fin.wagers,
            "you_answered": player_id in fin.answers,
            "your_wager": fin.wagers.get(player_id),
            "your_answer": fin.answers.get(player_id),
            "text": (self.game.clue(ROUND_FJ).text
                     if fin.step in (P.FINAL_CLUE, P.FINAL_ANSWERS,
                                     P.FINAL_REVEAL, P.FINAL_DONE) else None),
            "deadline": fin.deadline,
            "submitted": submitted,
        }

    _HANDLERS = {
        P.CMD_SET_CLOCK: _set_clock,
        P.CMD_SELECT_CLUE: _select_clue,
        P.CMD_BUZZ: _buzz,
        P.CMD_SELF_REPORT: _self_report,
        P.CMD_DD_WAGER: _dd_wager,
        P.CMD_FINAL_WAGER: _final_wager,
        P.CMD_FINAL_ANSWER: _final_answer,
        P.CMD_FINAL_REPORT: _final_report,
        P.CMD_HOST_START_ROUND: _start_round,
        P.CMD_HOST_NEXT_ROUND: _next_round,
        P.CMD_HOST_FORCE_RESOLVE: _force_resolve,
        P.CMD_HOST_REVEAL_RESPONSE: _reveal_answer,
        P.CMD_HOST_ADJUST_SCORE: _adjust_score,
        P.CMD_HOST_SET_CONTROL: _set_control,
        P.CMD_HOST_UNDO: _undo,
        P.CMD_HOST_KICK: _kick,
    }
