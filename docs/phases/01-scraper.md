# Phase 1 — Scraper

Implement `parse_game` and `fetch_game` in `jeopardy/scraper.py`, plus the `scrape.py` CLI.

## Acceptance
`python3 -m unittest tests.test_scraper -v` fully green. That suite is the spec;
read it first. Do not modify it or the fixtures.

## What the HTML looks like (verified against `tests/fixtures/game_9537.html`)

- Clue text: `<td id="clue_J_1_1" class="clue_text">…</td>`, immediately followed by
  `<td id="clue_J_1_1_r" …>` holding the answer. Rounds are `J` and `DJ`; columns and
  rows are 1-indexed, column-major in the id.
- The answer is the content of `<em class="correct_response">` inside the `_r` cell.
  That cell **also** contains judge banter, `[bracketed notes]`, contestant names and
  a nested score table — all of which must be excluded. Inner `<i>` tags are stripped.
- Daily Double: the header cell for that clue carries
  `<td class="clue_value_daily_double">DD: $1,200</td>`. **That figure is the
  contestant's wager, not the cell value.** Read the row's value from any other
  cell in the same row; `model.ROW_VALUES` is only a fallback. Never parse a value
  out of a DD cell. Pre-2001 shows used a $100-$500 board, so a hardcoded ladder
  silently doubles them.
- Categories: `<td class="category_name">`, six per round, in board order, inside
  that round's `<table class="round">`.
- Final Jeopardy: `td#clue_FJ` / `td#clue_FJ_r` within the `final_jeopardy_round`
  section; category from the `category_name` in `table.final_round`. Value is 0.
- Media: clue text may wrap a word in
  `<a href="https://www.j-archive.com/media/….jpg">`. Capture the URL in `clue.media`
  and keep the wrapped word in the clue text.
- Normalize: `<br>` becomes a space, tags are stripped, `html.unescape` is applied,
  runs of whitespace collapse to one, and the result is stripped.

## Requirements beyond the tests

- `fetch_game` accepts a full URL or a bare game id, uses `urllib.request` with a
  browser User-Agent, and raises `ScrapeError` on a non-200 or unparseable page.
- **Fail loudly on an incomplete board.** If any of the 60 clues or any answer is
  missing (unaired or unrevealed games), raise `ScrapeError` naming the missing ids
  rather than emitting a game with holes to be discovered mid-play.
- `scrape.py <url-or-id> [--out games/]` writes `games/<game_id>.json` and, when the
  game has media, downloads each asset to `games/<game_id>/media/<filename>` and
  rewrites `clue.media` to the relative local path so play needs no internet.
  Skip downloads that already exist. A media failure is a warning, not fatal.
- Verify against a second, unseen game id before declaring done.
