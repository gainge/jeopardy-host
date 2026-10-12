# Wire protocol

Names live in `jeopardy/protocol.py`; this file pins the **payload shapes**, which is
where four parallel implementations would otherwise drift. Phase 2 emits exactly these;
Phase 4 consumes exactly these.

Times are integer milliseconds in **server** time. Money is a plain integer of dollars.

## Transport

- Server -> client: SSE on `GET /events?token=T`. Each frame is
  `event: <type>\ndata: <json>\n\n`. An idle stream sends `event: ping` with
  `data: {}` every 10s. It is transport-level, has no `EVT_` constant and never
  reaches the reducer. Clients treat 25s of silence as a dead stream and
  reconnect, which replays `snapshot`.
- Every stream receives events in the order the engine produced them, and a
  `snapshot` is never followed by an event it already reflects. Reducers may rely
  on this: `answer_result` (a delta) always precedes the `clue_resolved` (absolute
  scores) it led to.
- Client -> server: `POST /api/command` with `{"type": …, …fields}`, validated by
  `protocol.validate_command`. Extra fields are dropped, missing fields rejected.

## Events

```jsonc
snapshot            // targeted, sent on connect and after reconnect; full replacement
{ "phase": "lobby|round|final|game_over", "round": "J|DJ|FJ|null",
  "you": "<player_id>", "is_host": false,
  "players": [ {"id","name","score","connected","is_host","rtt_ms","offset_ms"} ],
  "control": "<player_id>|null",
  "board": { "categories": ["…x6"],
             "cells": [ {"clue_id","col","row","value","available"} ] },
  "clue": null | { …clue_selected payload…,
                   "step": "reading|open|window|answering|dd_wager|dd_answering|resolved",
                   "answering": "<player_id>|null",
                   "attempted": ["<player_id>"],          // already missed; no rebound
                   "revealed_player_id": "<player_id>|null",
                   "your_revealed_answer": "…|null",      // see invariant 1
                   "resolved_answer": "…|null" },         // only on the answer screen
  "final": null | { "step","category","you_wagered":bool,"you_answered":bool,
                    "submitted": ["<player_id>"] } }

players             { "players": [ …as above… ] }
control_changed     { "player_id": "…" }
round_started       { "round": "J", "categories": [...], "cells": [...] }

clue_selected       { "clue_id","category","value","text","media":[...],
                      "unlock_at": 1730000000000, "selected_by": "<player_id>" }
                    // never carries answer or is_daily_double
buzzers_open        { "clue_id" }
buzz_registered     { "clue_id", "player_id" }        // order still undecided
buzz_result         { "clue_id",
                      "order": [ {"player_id","delta_ms","status","press_server"} ],
                      "rejected": [ {"player_id","status"} ] }
answering           { "clue_id","player_id","deadline" }
answer_result       { "clue_id","player_id","correct":bool,"score_delta":-400 }
clue_resolved       { "clue_id","answer","scores":{"<player_id>":1200} }
                    // the ONLY event that ever carries a clue answer. The clue
                    // stays on screen (step "resolved") until clue_dismissed.
clue_dismissed      { "clue_id" }      // host returned everyone to the board

dd_revealed         { "clue_id","player_id","min_wager","max_wager" }
dd_wager_set        { "clue_id","player_id","amount" }

final_category      { "category" }
final_wagers_open   { "participants": ["<player_id>"], "deadline" }
final_wager_received{ "player_id" }                    // who, never how much
final_clue          { "text","media":[...],"deadline" }
final_answer_received { "player_id" }                  // who, never what
final_reveal        { "answer",
                      "entries": [ {"player_id","wager","answer"} ] }
final_result        { "player_id","correct":bool,"score_delta":-2000,
                      "scores":{"<player_id>":9200} }

score_adjusted      { "player_id","delta","reason","by":"<host_id>","score":800 }
game_over           { "scores":{...}, "winner":"<player_id>" }
error               { "message": "…" }                 // targeted at the actor
```

## Redaction rules, restated as invariants

1. `answer` appears only in `clue_resolved` and `final_reveal`. The one earned
   exception is `answer_disclosed` (targeted at the current answer holder only,
   after `host_reveal_response`) and its `snapshot` mirror, `clue.your_revealed_answer`
   — never the bare `answer` key, and never sent to anyone but that one player.
   A snapshot taken while a resolved clue is still on screen carries
   `clue.resolved_answer`. It mirrors the `clue_resolved` broadcast everyone has
   already received, so it is the same for every recipient.
2. `is_daily_double` never appears in any payload. The fact is communicated solely by
   the presence of a `dd_revealed` event after selection.
3. Final wagers and answers appear only in `final_reveal`, and a player's own choices
   only in their own `snapshot`.
4. **The host is not a privileged recipient.** For identical state, the host's
   `snapshot.board` and `snapshot.clue` are byte-identical to any other player's,
   except for the same kind of per-recipient field Final Jeopardy already has
   (`your_wager`, `your_answer`) — `clue.your_revealed_answer` differs for the host
   exactly when the host happens to be the one currently answering, same as anyone
   else in that seat.

## HTTP endpoints (pinned during Phase 5 integration)

```jsonc
POST /api/join
  -> { "name": "Gibson", "admin_token": "…" }   // admin_token only when claiming host
  <- 200 { "player_id": "p1", "token": "…", "is_host": true }
  <- 400/403 { "message": "…" }

POST /api/command?token=T                        // also accepts X-Jeopardy-Token
  -> { "type": "buzz", … }
  <- 202 { "ok": true } | 400/401/403 { "message": "…" }

GET /api/time    <- { "server_ms": 1730000000000 }   // no token required
GET /events?token=T                                   // SSE; opens with `snapshot`
```

The first player to join is seated as host, and `admin_token` from the server console
is what authorizes that seat. It buys game management only — it is not a spoiler
channel, and the host's snapshot stays byte-identical to every other player's.
