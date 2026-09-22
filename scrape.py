#!/usr/bin/env python3
"""Turn a j-archive show page into a playable game artifact.

    python3 scrape.py https://j-archive.com/showgame.php?game_id=9537
    python3 scrape.py 9537 --out games/

Media is mirrored next to the artifact so a scraped game plays with the network
unplugged, which is the point of scraping ahead of game night.
"""

from __future__ import annotations

import argparse
import os
import sys

from jeopardy.scraper import ScrapeError, download_media, fetch_game


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("game", help="j-archive showgame URL or bare game id")
    parser.add_argument("--out", default="games", help="directory for the artifact (default: games)")
    parser.add_argument("--no-media", action="store_true", help="skip downloading media assets")
    args = parser.parse_args(argv)

    try:
        game = fetch_game(args.game)
    except ScrapeError as exc:
        print("scrape failed: %s" % exc, file=sys.stderr)
        return 1

    os.makedirs(args.out, exist_ok=True)
    assets = sum(len(c.media) for r in game.rounds.values() for c in r.clues.values())
    if assets and not args.no_media:
        for warning in download_media(
            game,
            os.path.join(args.out, game.game_id, "media"),
            link_prefix="%s/media" % game.game_id,
        ):
            print("warning: %s" % warning, file=sys.stderr)

    path = os.path.join(args.out, "%s.json" % game.game_id)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(game.to_json())

    clues = sum(len(r.clues) for r in game.rounds.values())
    dds = sum(1 for r in game.rounds.values() for c in r.clues.values() if c.is_daily_double)
    print("%s — %s (%s)" % (game.game_id, game.title, game.aired))
    print("  %d clues, %d daily doubles, %d media assets" % (clues, dds, assets))
    print("  wrote %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
