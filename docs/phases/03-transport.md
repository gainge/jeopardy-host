# Phase 3 — Transport

Implement `jeopardy/server.py`, `hub.py`, `scheduler.py`, `store.py`, the `serve.py`
CLI, and `tests/test_transport.py`.

## Acceptance
`python3 -m unittest tests.test_transport -v` fully green.

Code against the `Engine` **signatures** in `jeopardy/engine.py` (`apply`, `tick`,
`next_deadline`, `snapshot_for`, and the `Event` dataclass). Phase 2 is implementing
its body in parallel — use a fake engine in your tests; do not edit `engine.py`.

## Why SSE and not WebSockets
The stdlib has no WebSocket support and hand-rolling RFC 6455 is not worth the risk
here. Server->client is SSE; client->server is plain POST. This costs nothing for
fairness: buzz order is decided by timestamps carried *inside* the message, never by
arrival order.

## Routes
```
GET  /                -> static/index.html
GET  /static/*        -> static assets, correct MIME types, no-cache
GET  /media/*         -> scraped media for the loaded game
GET  /api/time        -> {"server_ms": N}
GET  /events?token=T  -> SSE stream
POST /api/join        -> {"name": …} -> {"player_id": …, "token": …, "is_host": bool}
POST /api/command     -> a protocol command; requires a token
```

- `/api/time` must read the clock and write the response **before any other work** in
  the handler, including logging and auth, to keep measured skew honest. It is the
  foundation of buzz fairness.
- `/api/command` decodes JSON, calls `protocol.validate_command`, resolves the token
  to a player, rejects `protocol.HOST_COMMANDS` from non-hosts, then applies to the
  engine **under a single global lock** and fans the resulting events out. Cap the
  request body size.
- Path traversal must be impossible on `/static/*` and `/media/*`.

## Hub
Per-client bounded `queue.Queue`. `broadcast` only ever enqueues — it must never write
to a socket, so one wedged client cannot stall the game loop. The SSE handler loops on
`q.get(timeout=15)` and writes a `: heartbeat` comment on timeout. Full queue or
`BrokenPipeError` -> reap the subscription and mark the player disconnected.
`ThreadingHTTPServer` with `daemon_threads = True`.

## Scheduler
One thread, one `threading.Condition`, one deadline heap. After every command, ask the
engine for `next_deadline()` and schedule it; when it fires, call `engine.tick(now)`
under the same lock and broadcast the result. Deliberately not a sprawl of
`threading.Timer` objects, so a rescheduled deadline cannot leave a stale timer racing
current state.

## Store
Append-only JSONL **command** journal at `games/<game_id>/events.jsonl` — commands plus
`__tick__` entries, each with actor, fields and `now_ms`. Because the engine is
deterministic, replay reproduces state exactly. `fsync` on append is acceptable; this
is a game with a handful of writes per minute. On boot, if a journal exists, replay it
and resume rather than starting fresh.

## serve.py
`python3 serve.py games/9537.json --host 0.0.0.0 --port 8080`. Generates the host admin
token with `secrets.token_urlsafe` and **prints it to the console along with the LAN
URL players should open**. Graceful shutdown on SIGINT flushes the journal.

## Tests
10 concurrent SSE subscribers all receive a broadcast; a wedged subscriber is reaped
without blocking the others; `/api/time` skew is small and monotonic; a journal
replays to identical state after a simulated crash; a non-host token is rejected for
every host command; malformed and oversized bodies are rejected.
