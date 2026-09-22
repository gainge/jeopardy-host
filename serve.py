#!/usr/bin/env python3
"""Run a scraped game.

    python3 serve.py games/9537.json --host 0.0.0.0 --port 8080

Prints the LAN URL to hand out and the admin token that claims the host seat.
The host plays like everyone else; the token buys game-management commands, not
a look at the answers.
"""

from __future__ import annotations

import argparse
import signal
import sys

from jeopardy import server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-hosted Jeopardy server")
    parser.add_argument("game", help="path to a scraped game JSON artifact")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--admin-token", default=None,
                        help="reuse a known token instead of generating one")
    args = parser.parse_args(argv)

    app = server.build(args.game, admin_token=args.admin_token)

    def shutdown(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.announce(app, args.host, args.port)
    try:
        app.serve_forever(args.host, args.port)
    except KeyboardInterrupt:
        print("\nshutting down; flushing journal")
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
