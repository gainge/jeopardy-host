# Self-hosted Jeopardy

A Jeopardy game you run on your own machine and play with coworkers over a video call.
Scrape a real episode from j-archive, hand out the URL, and play — **including the
person running it**, who is never shown answers or Daily Double locations.

Runtime requirement, end to end: `python3` and a browser. No pip, no npm, no build
step, nothing to get approved.

## Run it

```sh
python3 scrape.py 9537              # or a full j-archive showgame.php URL
python3 serve.py games/9537.json --host 0.0.0.0 --port 8080
```

The console prints the join URL and a host token. Open the URL, enter your name and
paste the token to claim the facilitator seat; everyone else just enters a name.

```sh
./run_tests.sh                      # 100+ tests, stdlib unittest only
```

## The three problems this solves

**Fair buzzing across a network.** Accepting whoever's packet lands first just rewards
whoever sits closest to the host. Instead, every client measures its offset from the
server clock (Cristian's algorithm against `/api/time`, keeping the minimum-RTT
sample), and a buzz carries the press time *translated into server time*. The first
buzz opens a 300ms reconciliation window; every buzz landing inside it is sorted by
press time. A player on a 200ms link who pressed first beats a local player who
pressed second — there's an end-to-end test that proves exactly that over real HTTP.

After each race everyone sees the millisecond deltas. Client timestamps are
forgeable, and the clamps in `buzzer.py` bound and flag backdating rather than
preventing it; the public readout is the real enforcement, which is consistent with
the honor system the rest of the game already runs on.

**The host plays too.** No client ever receives an answer or a Daily Double marker
until the server discloses it, and the host is not a privileged recipient — their
board and clue payloads are byte-identical to every other player's. A Daily Double is
revealed to everyone at the instant the cell is selected, which makes the host's
ignorance automatic rather than a special case. `redact.py` is the only path to a
client, and tests walk every payload for every recipient looking for leaks.

**Adjudication without a judge.** Since nobody can see answers, the player who wins
the buzz says it out loud on the call and then self-reports right or wrong. Wrong
passes to the next-fastest buzzer. The answer is disclosed to everyone only once the
clue fully resolves. The host keeps score-correction and undo for whatever the group
disagrees about.

## Layout

```
scrape.py  serve.py          CLIs
jeopardy/
  protocol.py                wire contract: names, payload validators, timings
  model.py                   Game / Round / Clue / Player / Buzz
  scraper.py                 j-archive HTML -> Game
  engine.py                  pure state machine: (state, command, now_ms) -> events
  redact.py                  the spoiler boundary
  buzzer.py                  clock offsets and buzz-order reconciliation
  hub.py  scheduler.py  store.py  server.py
static/                      vanilla ES modules, no build step
docs/                        PROTOCOL.md, ORCHESTRATION.md, phases/
```

Two invariants hold the design together, and both are enforced by tests rather than
convention:

1. **`engine.py` is pure** — no clock, no I/O, deterministic. That is what lets
   `store.py` replay a command journal to reproduce state exactly, which is where
   crash recovery and host undo come from.
2. **Nothing reaches a client except through `redact.py`.**

## Things worth knowing

- **j-archive's media assets are all 404 now.** The `<img>` links are still in the
  pages; the files are gone. Clue media silently drops rather than showing a broken
  image. Nothing else depends on it.
- **Some archived games are genuinely incomplete** — clues never revealed on air.
  `scrape.py` refuses those by name rather than letting you find the hole mid-game,
  so expect to try a couple of ids. (6500 and 8000 are both incomplete; 9537, 7000,
  8800 are clean.)
- **Pre-2001 shows used a $100–$500 board.** Values are read off the page, not
  assumed, so older games score as they aired.
- **j-archive's disclaimer asks people not to scrape it.** It's a fan-run site and
  this is a private tool for a handful of people, but it's your call to make
  knowingly. Be polite: scrape a game once and play it from the saved JSON.
- **Behind a reverse proxy, turn off buffering for `/events`.** The game streams
  over SSE with a ping every 10s, and clients reconnect if the stream goes quiet
  for 25s. nginx honours the `X-Accel-Buffering: no` header the server already
  sends, but a proxy that buffers or idles out long responses needs, in nginx terms,
  `proxy_buffering off;` and `proxy_read_timeout` well above 10s on that location.
- **The game journal (`games/<id>/events.jsonl`) belongs to the server version
  that wrote it.** A resolved clue now waits for the host's "Back to board", so a
  journal from before that change will not replay cleanly. Start a fresh game
  after upgrading.
