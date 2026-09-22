# Phase 4 — Browser client

Implement `static/index.html`, `app.js`, `board.js`, `buzzer.js`, `host.js`,
`clock.js`, `style.css`, and `tests/test_client.py`.

## Constraints
Vanilla ES modules, no build step, no npm, no CDN. Must work offline. Assume a
laptop screen; this is played on a video call, so legibility at a glance matters more
than density.

## Clock sync (`clock.js`) — the foundation of fair buzzing
Cristian's algorithm against `GET /api/time`: for each of ~7 samples record
`t_send`, read `server_ms`, record `t_recv`; `rtt = t_recv - t_send`,
`offset = server_ms - (t_send + rtt/2)`. Keep the offset from the **minimum-RTT**
sample. Re-sync every 30s and on reconnect. Report the result via `set_clock` so the
host panel can show connection quality. Use `performance.now()` deltas against one
anchored `Date.now()` reading so a mid-game OS clock adjustment cannot corrupt
measurements.

## Buzzing (`buzzer.js`)
- On `clue_selected`, start a countdown to `unlock_at - offset` **in local time**, so
  every client unlocks at the same real instant regardless of latency.
- Spacebar and a large click target. Record the press with the same monotonic source
  as the clock sync, convert to server time, POST `buzz` with `press_server`,
  `offset_ms`, `rtt_ms`.
- A press before unlock is ignored locally *and* server-side; show a brief "too early"
  hint and let them press again immediately. No lockout penalty.
- **Fire the POST and update the UI optimistically in the same frame** — never await
  the network before showing feedback, or you reintroduce the latency bias the whole
  design exists to remove.
- After `buzz_result`, show the race readout: every buzzer with their millisecond
  delta. This is the transparency that makes the honor system work.

## Board (`board.js`)
Six columns, categories on top, values in the grid, spent cells blanked. Clue view
takes over the screen. Render media from `clue.media`. Scores strip along the bottom
with connection indicators.

## Host panel (`host.js`)
Shown only when `is_host`. Score adjust (+/- with a reason), undo, force-resolve,
set control, kick, start/advance round, and the per-player RTT/offset readout with
any flagged buzzes. **It must render zero clue answers** — the server never sends
the host any, and the panel must not imply otherwise.

## Self-report flow
When you win the buzz, you get two large buttons — "I was right" / "I was wrong" —
and everyone else sees that you are answering. Answers are spoken on the call; the
one exception is Final Jeopardy, which has a text input.

## Reconnect
Persist `{player_id, token}` in `localStorage`. On `EventSource` error, reconnect with
backoff and apply the `snapshot` event as a full state replacement. A refresh
mid-game must cost nothing.

## Tests
`tests/test_client.py` may be light — assert the static files exist, parse as valid
HTML/JS (no syntax errors via a `node --check` fallback that is skipped if node is
absent), reference only protocol event names that exist in `protocol.py`, and contain
no `http://` or `https://` references to external hosts (the offline requirement).
