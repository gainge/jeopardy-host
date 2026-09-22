"""ThreadingHTTPServer wiring: routing, SSE, static files, engine lock.

Routes:
  GET  /                -> static/index.html
  GET  /static/*        -> static assets
  GET  /api/time        -> {"server_ms": N}; answered before any other work
  GET  /events?token=   -> SSE stream
  POST /api/command     -> validated by protocol.validate_command, applied under
                           a single engine lock, resulting events fanned out
  POST /api/join        -> issues a player token
"""

from __future__ import annotations


def run(game_path: str, host: str, port: int) -> None:
    """Boot the server and print the host admin token to the console."""
    raise NotImplementedError
