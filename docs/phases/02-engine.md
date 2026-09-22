# Phase 2 — Game engine

Implement `jeopardy/engine.py`, `jeopardy/redact.py`, `jeopardy/buzzer.py`, and write
`tests/test_engine.py`.

## Acceptance
`python3 -m unittest tests.test_redact tests.test_engine -v` fully green.
`tests/test_redact.py` is a spec you satisfy, not a file you edit.

## Purity contract — non-negotiable
`engine.py` imports nothing from `time`, `socket`, `http`, `threading`, `random`,
`urllib`, `datetime`, `os`, or `secrets` (asserted by `PurityTest`). Every
time-dependent decision takes `now_ms`. The engine is a deterministic function of
(state, command, now_ms) so that `store.py` can replay a journal and reproduce state
exactly — this is what makes crash recovery and host undo work.

Player ids must therefore be derived deterministically (e.g. a counter, or a hash of
name + join sequence), never from `uuid`/`random`.

## State machine

`lobby -> round(J) -> round(DJ) -> final -> game_over`

Clue lifecycle:
`reading -> open -> window -> answering -> (next buzzer | resolved)`

- On `select_clue`: validate the actor holds control and the cell is unspent. Emit
  `clue_selected` carrying text, value, category, media, and `unlock_at = now +
  protocol.read_ms_for(text)`. Set step `reading`.
- Deadline `unlock_at` fires -> step `open`, emit `buzzers_open`.
- First accepted buzz -> step `window`, set `window_closes_at = now + BUZZ_WINDOW_MS`.
  Emit `buzz_registered` with the player id only — **never the order**, which is still
  undecided.
- `window_closes_at` fires -> `buzzer.resolve_order` -> emit `buzz_result` with the
  ordered readout (player_id, delta_ms, status) so the group can see the race, then
  step `answering` for the winner with `answer_deadline = now + ANSWER_TIMEOUT_MS`.
- `self_report`: only the player currently answering may send it. Correct -> award
  `+value`, resolve the clue, give them control. Incorrect -> deduct `value`, advance
  to the next buzzer in the order; if the order is exhausted, reopen buzzers for
  players who have not yet attempted, or resolve if none remain.
- `answer_deadline` fires -> treat as an incorrect self-report with no score change
  (they never answered), and move on.
- On resolution: mark the cell used, emit `clue_resolved` **carrying the answer** —
  this is the only event that ever contains one. When every cell is spent, the round
  ends.

## Daily Doubles
`is_daily_double` stays hidden until the cell is selected; then emit `dd_revealed` to
everyone simultaneously, which is what keeps the host unspoiled without a special
case. The selector alone plays it, no buzzing. Wager bounds: minimum
`DD_MIN_WAGER`, maximum `max(current_score, top value of the current round)`.
Emit `dd_wager_set` with the amount (wagers are announced on the real show).

## Final Jeopardy
`category -> wagers -> clue -> answers -> reveal -> done`.
Only players with a positive score participate. Wagers are sealed (0..score) and
answers are sealed until every participant has submitted or `FINAL_ANSWER_MS` expires;
`final_wager_received` / `final_answer_received` announce *who* has submitted, never
*what*. `final_reveal` then discloses all wagers, all answers, and the correct answer
at once. Each player self-reports; the host can override.

## Host powers
`host_adjust_score` (any delta, with a reason, broadcast so it is visible),
`host_force_resolve`, `host_set_control`, `host_undo`, `host_kick`, round control.
None of these read or return clue answers. `host_undo` reverses the last scoring
event by appending a compensating change — never by rewriting history.

## Buzzer module
- `classify` implements the clamps documented in its docstring using the tolerances
  in `protocol.py`. Rejected buzzes are recorded with a status, not dropped, so the
  host panel can show them.
- `resolve_order` sorts by effective press time; exact ties break on
  `sha256(player_id + clue_id)` so order never depends on arrival.
- `estimate_offset` returns the offset from the minimum-RTT sample.

## Tests you must write
A full scripted game: both rounds, a Daily Double, a wrong-then-right buzz chain,
an answer timeout, a host score correction, an undo, and Final Jeopardy. Plus buzzer
unit tests covering early / future / backdated / tie cases, and a test that a
*later-arriving* buzz with an *earlier* press time wins.
