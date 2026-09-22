"""j-archive.com -> Game.

Structure this relies on (verified against game 9537):
  * clue text   td#clue_{J|DJ}_{col}_{row}.clue_text
  * answer      the sibling td#clue_..._r, inside <em class="correct_response">
  * daily double the header cell's class is clue_value_daily_double, and its text
                is the contestant's WAGER, not the board value — so the cell value
                is read from another cell in the same row (see _row_values),
                never from the Daily Double cell itself
  * final       td#clue_FJ / td#clue_FJ_r, category in table.final_round
  * media       absolute links to j-archive.com/media/*, downloaded locally so
                gameplay needs no internet

The answer cell also holds judge banter and contestant names; only the
correct_response span is extracted.

Parsing is regex over the raw source rather than html.parser: j-archive emits
nested tables inside the very cells we care about, and a stack-based parser buys
nothing over anchoring on the stable `id` attributes the site has used for
two decades.
"""

from __future__ import annotations

import html
import os
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request

from .model import (
    BOARD_COLS,
    BOARD_ROWS,
    ROUND_DJ,
    ROUND_FJ,
    ROUND_J,
    ROW_VALUES,
    Clue,
    Game,
    Round,
    clue_id,
)

SHOW_URL = "https://j-archive.com/showgame.php?game_id={}"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
FETCH_TIMEOUT_S = 30

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_MEDIA_RE = re.compile(r'<a\s[^>]*href="([^"]+)"', re.I)
_CORRECT_RE = re.compile(
    r'<em class="correct_response">(.*?)</em>', re.I | re.S
)
_CATEGORY_RE = re.compile(r'<td class="category_name">(.*?)</td>', re.I | re.S)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
_AIRED_RE = re.compile(r"aired\s+(\d{4}-\d{2}-\d{2})", re.I)
_GAME_ID_RE = re.compile(r"chartgame\.php\?game_id=(\d+)")
_PAGE_ERROR_RE = re.compile(r'<p class="error">(.*?)</p>', re.I | re.S)

_ROUND_SECTIONS = {
    ROUND_J: "jeopardy_round",
    ROUND_DJ: "double_jeopardy_round",
    ROUND_FJ: "final_jeopardy_round",
}


class ScrapeError(RuntimeError):
    """The page was missing clues or otherwise unusable. Fail loudly at scrape
    time rather than discovering a hole mid-game."""


def _text(fragment: str) -> str:
    """Tags out, entities in, whitespace flattened."""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", _BR_RE.sub(" ", fragment)))).strip()


def _section(html_text: str, div_id: str) -> str:
    start = html_text.find('<div id="%s"' % div_id)
    if start < 0:
        raise ScrapeError(
            "page has no %s section — the game exists but its clues have not been "
            "transcribed yet" % div_id
        )
    end = html_text.find('<div id="', start + 1)
    return html_text[start:end if end > 0 else len(html_text)]


def _cell(section: str, cell_id: str) -> str | None:
    """Contents of td#<cell_id>, bounded by the next id= attribute.

    The answer cells nest a score table, so the closing tag cannot be trusted;
    the next id-bearing element is the reliable fence.
    """
    match = re.search(r'<td id="%s"[^>]*>' % re.escape(cell_id), section)
    if not match:
        return None
    rest = section[match.end():]
    fence = re.search(r'<(?:td|var|div)\s[^>]*id="', rest)
    body = rest[:fence.start()] if fence else rest
    tail = body.rfind("</td>")
    return body[:tail] if tail >= 0 else body


def _media(fragment: str, base_url: str) -> list[str]:
    urls = []
    for href in _MEDIA_RE.findall(fragment):
        href = html.unescape(href)
        if "/media/" not in href:
            continue
        urls.append(urllib.parse.urljoin(base_url or SHOW_URL.format(0), href))
    return urls


def _answer(section: str, cell_id: str) -> str:
    body = _cell(section, cell_id)
    if body is None:
        return ""
    found = _CORRECT_RE.search(body)
    return _text(found.group(1)) if found else ""


def _is_daily_double(section: str, cid: str) -> bool:
    """The header cell sits just above the clue; anchor on its _stuck marker."""
    match = re.search(
        r'id="clue_%s_stuck"[^>]*>.*?<td class="(clue_value[^"]*)"'
        % re.escape(cid),
        section,
        re.S,
    )
    return bool(match) and match.group(1) == "clue_value_daily_double"


def _row_values(section: str, name: str) -> list[int]:
    """The dollar value of each row, read from the page rather than assumed.

    Shows before the 2001-11-26 doubling used a $100-$500 board, so a hardcoded
    ladder silently misprices them. Daily Double cells are skipped because they
    render the contestant's wager instead of the cell value; any other cell in the
    same row carries the real figure. ROW_VALUES remains the fallback for a row
    whose every cell is unreadable.
    """
    values = []
    for row in range(1, BOARD_ROWS + 1):
        found = None
        for col in range(1, BOARD_COLS + 1):
            match = re.search(
                r'id="clue_%s_%d_%d_stuck"[^>]*>.*?<td class="clue_value">\s*\$?([\d,]+)'
                % (re.escape(name), col, row),
                section,
                re.S,
            )
            if match:
                found = int(match.group(1).replace(",", ""))
                break
        values.append(found if found else ROW_VALUES[name][row - 1])
    return values


def _parse_board_round(html_text: str, name: str, source_url: str) -> tuple[Round, list[str]]:
    section = _section(html_text, _ROUND_SECTIONS[name])
    categories = [_text(c) for c in _CATEGORY_RE.findall(section)]
    row_values = _row_values(section, name)
    if len(categories) != BOARD_COLS:
        raise ScrapeError(
            "%s round has %d categories, expected %d" % (name, len(categories), BOARD_COLS)
        )

    clues: dict[str, Clue] = {}
    missing: list[str] = []
    for col in range(1, BOARD_COLS + 1):
        for row in range(1, BOARD_ROWS + 1):
            cid = clue_id(name, col, row)
            raw = _cell(section, "clue_%s" % cid)
            answer = _answer(section, "clue_%s_r" % cid)
            text = _text(raw) if raw is not None else ""
            if not text or not answer:
                missing.append(cid)
                continue
            clues[cid] = Clue(
                id=cid,
                round=name,
                col=col,
                row=row,
                category=categories[col - 1],
                text=text,
                answer=answer,
                value=row_values[row - 1],
                is_daily_double=_is_daily_double(section, cid),
                media=_media(raw, source_url),
            )
    return Round(name=name, categories=categories, clues=clues), missing


def _parse_final(html_text: str, source_url: str) -> tuple[Round, list[str]]:
    section = _section(html_text, _ROUND_SECTIONS[ROUND_FJ])
    categories = [_text(c) for c in _CATEGORY_RE.findall(section)]
    if not categories:
        raise ScrapeError("final round has no category")
    raw = _cell(section, "clue_FJ")
    answer = _answer(section, "clue_FJ_r")
    text = _text(raw) if raw is not None else ""
    if not text or not answer:
        return Round(name=ROUND_FJ, categories=categories[:1], clues={}), [ROUND_FJ]
    clue = Clue(
        id=ROUND_FJ,
        round=ROUND_FJ,
        col=0,
        row=0,
        category=categories[0],
        text=text,
        answer=answer,
        value=0,
        media=_media(raw, source_url),
    )
    return Round(name=ROUND_FJ, categories=categories[:1], clues={ROUND_FJ: clue}), []


def parse_game(html_text: str, source_url: str = "") -> Game:
    """Parse a saved showgame.php page. Pure — no network, so it is fixture-testable."""
    page_error = _PAGE_ERROR_RE.search(html_text)
    if page_error:
        raise ScrapeError(_text(page_error.group(1)))

    title_match = _TITLE_RE.search(html_text)
    if not title_match:
        raise ScrapeError("page has no <title>; not a showgame.php document")
    title = _text(title_match.group(1))
    # "J! Archive - Show #9620, aired 2026-09-18"
    if " - " in title:
        title = title.split(" - ", 1)[1].strip()
    aired_match = _AIRED_RE.search(title) or _AIRED_RE.search(html_text)
    if not aired_match:
        raise ScrapeError("could not determine the air date")

    game_id = ""
    if source_url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(source_url).query)
        game_id = (query.get("game_id") or [""])[0]
    if not game_id:
        id_match = _GAME_ID_RE.search(html_text)
        if not id_match:
            raise ScrapeError("could not determine the j-archive game id")
        game_id = id_match.group(1)

    rounds: dict[str, Round] = {}
    missing: list[str] = []
    for name in (ROUND_J, ROUND_DJ):
        rnd, gaps = _parse_board_round(html_text, name, source_url)
        rounds[name] = rnd
        missing.extend(gaps)
    final, gaps = _parse_final(html_text, source_url)
    rounds[ROUND_FJ] = final
    missing.extend(gaps)

    if missing:
        raise ScrapeError(
            "game %s is incomplete — no clue or answer for: %s"
            % (game_id, ", ".join(missing))
        )

    return Game(
        game_id=game_id,
        title=title,
        aired=aired_match.group(1),
        source_url=source_url,
        rounds=rounds,
    )


def game_url(url_or_id: str) -> str:
    """Accept a full showgame URL or a bare numeric game id."""
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return SHOW_URL.format(url_or_id)
    parsed = urllib.parse.urlparse(url_or_id)
    if parsed.scheme not in ("http", "https") or "j-archive.com" not in parsed.netloc:
        raise ScrapeError("expected a j-archive.com showgame URL or a game id: %r" % url_or_id)
    return url_or_id


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            if response.status != 200:
                raise ScrapeError("%s returned HTTP %s" % (url, response.status))
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ScrapeError("%s returned HTTP %s" % (url, exc.code)) from None
    except urllib.error.URLError as exc:
        raise ScrapeError("could not reach %s: %s" % (url, exc.reason)) from None


def fetch_game(url_or_id: str, media_dir: str | None = None) -> Game:
    """Fetch via urllib, parse, and optionally download media alongside."""
    url = game_url(url_or_id)
    body = _get(url)
    game = parse_game(body.decode("utf-8", "replace"), url)
    if media_dir is not None:
        download_media(game, media_dir)
    return game


def download_media(game: Game, media_dir: str, link_prefix: str | None = None) -> list[str]:
    """Mirror every remote asset locally and rewrite clue.media to point at it.

    `link_prefix` is what the rewritten clue.media entries are joined onto — the
    path the browser will request, which is not the filesystem path the file was
    written to. Left unset, the filesystem path is stored instead.

    A missing asset degrades that one clue rather than failing the scrape, since
    the clue text still stands on its own in nearly every case.
    """
    warnings: list[str] = []
    for rnd in game.rounds.values():
        for clue in rnd.clues.values():
            local: list[str] = []
            for url in clue.media:
                name = os.path.basename(urllib.parse.urlparse(url).path)
                if not name:
                    warnings.append("%s: unusable media URL %s" % (clue.id, url))
                    local.append(url)
                    continue
                path = os.path.join(media_dir, name)
                if not os.path.exists(path):
                    # Fetch fully before creating the file, so a failed download
                    # leaves no zero-byte stub for the next run to treat as cached.
                    try:
                        blob = _get(url)
                        os.makedirs(media_dir, exist_ok=True)
                        with open(path, "wb") as fh:
                            fh.write(blob)
                    except (ScrapeError, OSError) as exc:
                        warnings.append("%s: %s (%s)" % (clue.id, url, exc))
                        local.append(url)
                        continue
                local.append(
                    path if link_prefix is None else posixpath.join(link_prefix, name)
                )
            clue.media = local
    return warnings
