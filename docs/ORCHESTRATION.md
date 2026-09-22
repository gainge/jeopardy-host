# Orchestration

## Absolute constraints (apply to every phase)

1. **Python 3 standard library only.** No pip, no virtualenv, no third-party imports.
   Target machines are company-provisioned; every dependency costs installation clearance.
2. **Vanilla JS in the browser.** No npm, no bundler, no build step, no CDN frameworks.
   The app must work offline once a game is scraped.
3. `jeopardy/protocol.py` and `jeopardy/model.py` are the frozen contract. If a phase
   believes the contract is wrong, it reports the problem — it does not unilaterally
   edit those two files, because three other phases are compiling against them.
   (This was a phased, multi-implementer build constraint. Now that Phases 0-5 are
   done and there is one implementer, the protocol is amended directly when a real
   gameplay need arises — see `answer_disclosed`/`host_reveal_response` in
   `docs/PROTOCOL.md` for the first such amendment.)
4. **The host plays.** No code path may give the host earlier or richer knowledge than
   any other player. There is no "host view" of answers or Daily Doubles.

## Dependency graph

```
Phase 0 — contracts  (DONE, committed)
   |
   +--> Phase 1  scraper      jeopardy/scraper.py, scrape.py
   +--> Phase 2  engine       jeopardy/{engine,redact,buzzer}.py
   +--> Phase 3  transport    jeopardy/{server,hub,scheduler,store}.py, serve.py
   +--> Phase 4  client       static/*
             |
             +--> Phase 5  integration + end-to-end fairness proof
                      |
                      +--> Phase 6  playtest polish
```

Phases 1–4 run in parallel and touch disjoint files. Phase 3 codes against the
`Engine` signatures in `jeopardy/engine.py`, not its behavior, so it does not wait
on Phase 2.

## Gates

| Phase | Verification command | Passes when |
|---|---|---|
| 1 | `python3 -m unittest tests.test_scraper -v` | all green, including the golden match |
| 2 | `python3 -m unittest tests.test_redact tests.test_engine -v` | all green; purity test green |
| 3 | `python3 -m unittest tests.test_transport -v` | all green |
| 4 | `python3 -m unittest tests.test_client -v` + manual load | board renders from a recorded stream |
| 5 | `./run_tests.sh` | every suite green, including `test_e2e` |

## Ownership map — do not write outside your column

| Phase | Owns | Reads only |
|---|---|---|
| 1 | `jeopardy/scraper.py`, `scrape.py`, `tests/test_scraper.py`* | `model.py`, fixtures |
| 2 | `jeopardy/engine.py`, `redact.py`, `buzzer.py`, `tests/test_engine.py` | `protocol.py`, `model.py` |
| 3 | `jeopardy/server.py`, `hub.py`, `scheduler.py`, `store.py`, `serve.py`, `tests/test_transport.py` | `protocol.py`, `engine.py` signatures |
| 4 | `static/*`, `tests/test_client.py` | `protocol.py`, `docs/PROTOCOL.md` |

\* Phase 1 may not weaken `tests/test_scraper.py`; it is the acceptance spec.
