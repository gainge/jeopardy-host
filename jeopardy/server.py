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

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import protocol as P
from .engine import Engine
from .hub import Hub
from .model import Game
from .scheduler import Scheduler, now_ms
from .store import Store, TICK as TICK_ENTRY

MAX_BODY = 8192
TOKEN_HEADER = "X-Jeopardy-Token"

# Transport-owned journal entries. Double underscores keep them out of the
# protocol's command namespace, and store.replay skips them.
TOKEN_ENTRY = "__token__"

# ES modules are refused by strict browsers under a text/plain MIME type, and
# platform mimetypes databases disagree about .js and .mjs.
MIME_OVERRIDES = {
    ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".html": "text/html; charset=utf-8",
    ".json": "application/json", ".svg": "image/svg+xml",
}


class AuthError(Exception):
    """No token, or a token that names no player."""


class ForbiddenError(Exception):
    """A real player asked for something only the host may do."""


def safe_path(root: str, rel: str) -> str | None:
    """Resolve `rel` under `root`, or None if it escapes. Symlinks are resolved
    first so a link planted inside the tree cannot be used as a way out."""
    root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, unquote(rel).lstrip("/\\")))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


class GameServer:
    """Owns the engine lock. Every mutation in the process passes through here."""

    def __init__(self, engine: Engine, store: Store, *, static_dir: str,
                 media_dir: str, admin_token: str) -> None:
        self.engine = engine
        self.store = store
        self.static_dir = static_dir
        self.media_dir = media_dir
        self.admin_token = admin_token
        self.lock = threading.RLock()
        self.hub = Hub(on_reap=self._on_reap)
        self.scheduler = Scheduler(self._on_due)
        self.tokens: dict[str, str] = {}
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def host_id(self) -> str | None:
        """Derived from engine state, never tracked separately: the engine decides
        who is host, and a second copy of that fact could disagree after a replay."""
        state = getattr(self.engine, "state", None)
        for pid, player in (getattr(state, "players", None) or {}).items():
            if player.is_host:
                return pid
        return None

    # --- lifecycle ---------------------------------------------------------

    def resume(self) -> None:
        """Rebuild state and seats from the journal. Tokens are restored too, so a
        crash does not cost every player their seat and score."""
        with self.lock:
            self.store.replay(self.engine)
            for entry in self.store.entries():
                if entry["type"] == TOKEN_ENTRY:
                    self.tokens[entry["fields"]["token"]] = entry["fields"]["player_id"]
            self.scheduler.schedule(self.engine.next_deadline())

    def bind(self, host: str, port: int) -> int:
        """Bind and return the actual port, so port 0 is usable."""
        self._httpd = ThreadingHTTPServer((host, port), make_handler(self))
        self._httpd.daemon_threads = True
        return self._httpd.server_address[1]

    def serve_forever(self, host: str | None = None, port: int | None = None) -> None:
        if self._httpd is None:
            self.bind(host or "0.0.0.0", port or 8080)
        self.scheduler.start()
        self._httpd.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        self.scheduler.stop()
        self.hub.close_all()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            # shutdown() blocks until serve_forever's loop exits, and a server
            # that was bound but never served would never reach it.
            threading.Thread(target=self._teardown, args=(httpd,),
                             daemon=True).start()
        self.store.close()

    @staticmethod
    def _teardown(httpd: ThreadingHTTPServer) -> None:
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()

    # --- actions -----------------------------------------------------------

    def join(self, name: str, admin_token: str | None = None) -> dict[str, Any]:
        """Seat a player and issue their token.

        The engine seats the first arrival as host, so the admin token guards the
        door rather than the seat: without it, the first join is refused outright.
        That keeps the printed token meaningful without a second opinion about who
        the host is. The token buys game management only — the host's snapshot is
        byte-identical to everyone else's.
        """
        with self.lock:
            fields = {"name": name}
            claims_host = not getattr(self.engine.state, "players", {})
            if claims_host and not (self.admin_token and admin_token
                                    and secrets.compare_digest(admin_token,
                                                               self.admin_token)):
                raise ForbiddenError("the first seat is the host seat and needs "
                                     "the admin token from the server console")
            now = now_ms()
            # Write-ahead: the journal is the reducer's input, so it records the
            # command before the engine is allowed to have seen it.
            self.store.append(None, P.CMD_JOIN, fields, now)
            events = self.engine.apply(None, P.CMD_JOIN, fields, now)
            player_id = self._joined_id(events)
            token = secrets.token_urlsafe(24)
            self.store.append(player_id, TOKEN_ENTRY,
                              {"player_id": player_id, "token": token}, now)
            self.tokens[token] = player_id
            is_host = player_id == self.host_id
            self.scheduler.schedule(self.engine.next_deadline())
        self._fanout(events)
        return {"player_id": player_id, "token": token, "is_host": is_host}

    def _joined_id(self, events) -> str:
        """The engine mints ids deterministically so replay lands on the same ones;
        it reports the new one on its roster event."""
        for event in events or ():
            if event.type == P.EVT_PLAYERS and "player_id" in event.payload:
                return event.payload["player_id"]
        order = getattr(self.engine.state, "order", None) or []
        if not order:
            raise RuntimeError("engine seated no player for a join")
        return order[-1]

    def resolve(self, token: str | None) -> str:
        if not token:
            raise AuthError("missing token")
        player_id = self.tokens.get(token)
        if player_id is None:
            raise AuthError("unknown token")
        return player_id

    def command(self, token: str | None, payload: Any) -> None:
        player_id = self.resolve(token)
        ctype, fields = P.validate_command(payload)
        if ctype == P.CMD_JOIN:
            raise P.ProtocolError("join is issued by POST /api/join")
        if ctype in P.HOST_COMMANDS and player_id != self.host_id:
            self.hub.send_to(player_id, P.EVT_ERROR,
                             {"message": f"{ctype} is host-only"})
            raise ForbiddenError(ctype)
        with self.lock:
            now = now_ms()
            self.store.append(player_id, ctype, fields, now)
            events = self.engine.apply(player_id, ctype, fields, now)
            self.scheduler.schedule(self.engine.next_deadline())
        self._fanout(events)

    def attach(self, player_id: str):
        """Subscribe a stream and seed it with a snapshot. The snapshot is queued
        after the subscription exists, so no event can slip between the two."""
        sub = self.hub.subscribe(player_id)
        with self.lock:
            self._set_connected(player_id, True)
            snapshot = self.engine.snapshot_for(player_id)
        self.hub.send_to(player_id, P.EVT_SNAPSHOT, snapshot)
        self.hub.broadcast(P.EVT_PLAYERS, self.roster())
        return sub

    def detach(self, sub) -> None:
        self.hub.unsubscribe(sub)
        if sub.player_id not in self.hub.connected_ids():
            self._on_reap(sub.player_id)

    def roster(self) -> dict[str, Any]:
        with self.lock:
            state = getattr(self.engine, "state", None)
            players = getattr(state, "players", None) or {}
            order = getattr(state, "order", None) or list(players)
            return {"players": [players[pid].to_dict() for pid in order
                                if pid in players]}

    # --- internals ---------------------------------------------------------

    def _fanout(self, events) -> None:
        for event in events or ():
            if event.targets is None:
                self.hub.broadcast(event.type, event.payload)
                continue
            for pid in event.targets:
                self.hub.send_to(pid, event.type, event.payload)

    def _on_due(self, now: int) -> None:
        with self.lock:
            self.store.append(None, TICK_ENTRY, {}, now)
            events = self.engine.tick(now)
            self.scheduler.schedule(self.engine.next_deadline())
        self._fanout(events)

    def _on_reap(self, player_id: str) -> None:
        with self.lock:
            changed = self._set_connected(player_id, False)
        if changed:
            self.hub.broadcast(P.EVT_PLAYERS, self.roster())

    def _set_connected(self, player_id: str, value: bool) -> bool:
        """Connection liveness is transport state, not a game rule, so it is not
        journaled and the engine is not asked to decide it."""
        state = getattr(self.engine, "state", None)
        player = (getattr(state, "players", None) or {}).get(player_id)
        if player is None or player.connected == value:
            return False
        player.connected = value
        return True


# --- HTTP layer -------------------------------------------------------------

def make_handler(app: GameServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jeopardy/0.1"

        def log_message(self, fmt, *args):
            """Silent by default: request logging on the buzz path is measurable,
            and a console full of /api/time hides the join URL the host needs."""

        # -- GET

        def do_GET(self):
            if self.path == "/api/time":
                # Deliberately first, allocation- and logging-free: every client's
                # clock offset is measured against this timestamp.
                body = b'{"server_ms":%d}' % int(time.time() * 1000)
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Cache-Control: no-store\r\nContent-Length: %d\r\n\r\n%s"
                    % (len(body), body))
                return

            parts = urlparse(self.path)
            path = parts.path
            if path == "/events":
                token = parse_qs(parts.query).get("token", [None])[0]
                return self._sse(token)
            if path in ("/", "/index.html"):
                return self._file(app.static_dir, "index.html")
            if path.startswith("/static/"):
                return self._file(app.static_dir, path[len("/static/"):])
            if path.startswith("/media/"):
                return self._file(app.media_dir, path[len("/media/"):])
            self._json(404, {"message": "not found"})

        # -- POST

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
            except ValueError as exc:
                # The declared body was never drained, so this connection can no
                # longer be framed.
                self.close_connection = True
                return self._json(413, {"message": str(exc)}, close=True)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"message": "body must be JSON"})
            if not isinstance(payload, dict):
                return self._json(400, {"message": "body must be a JSON object"})

            if path == "/api/join":
                try:
                    _, fields = P.validate_command({"type": P.CMD_JOIN,
                                                    "name": payload.get("name")})
                except P.ProtocolError as exc:
                    return self._json(400, {"message": str(exc)})
                admin = payload.get("admin_token")
                try:
                    seat = app.join(fields["name"],
                                    admin if isinstance(admin, str) else None)
                except ForbiddenError as exc:
                    return self._json(403, {"message": str(exc)})
                return self._json(200, seat)

            if path == "/api/command":
                try:
                    app.command(self._token(payload), payload)
                except AuthError as exc:
                    return self._json(401, {"message": str(exc)})
                except ForbiddenError as exc:
                    return self._json(403, {"message": f"{exc} is host-only"})
                except P.ProtocolError as exc:
                    return self._json(400, {"message": str(exc)})
                return self._json(202, {"ok": True})

            self._json(404, {"message": "not found"})

        # -- helpers

        def _token(self, payload: dict[str, Any]) -> str | None:
            token = self.headers.get(TOKEN_HEADER)
            if not token:
                token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
            if not token:
                value = payload.get("token")
                token = value if isinstance(value, str) else None
            return token

        def _body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad Content-Length") from None
            if length > MAX_BODY:
                raise ValueError(f"body exceeds {MAX_BODY} bytes")
            data = self.rfile.read(length) if length > 0 else b""
            if len(data) > MAX_BODY:
                raise ValueError(f"body exceeds {MAX_BODY} bytes")
            return data

        def _json(self, code: int, payload: dict[str, Any],
                  close: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            if close:
                self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, root: str, rel: str) -> None:
            target = safe_path(root, rel)
            if target is None or not os.path.isfile(target):
                return self._json(404, {"message": "not found"})
            ext = os.path.splitext(target)[1].lower()
            ctype = (MIME_OVERRIDES.get(ext)
                     or mimetypes.guess_type(target)[0]
                     or "application/octet-stream")
            with open(target, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            # A scraped game is edited between rounds; a cached board is worse
            # than a re-fetch on a LAN.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _sse(self, token: str | None) -> None:
            try:
                player_id = app.resolve(token)
            except AuthError as exc:
                return self._json(401, {"message": str(exc)})
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            sub = app.attach(player_id)
            try:
                for frame in sub.stream():
                    self.wfile.write(frame)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.detach(sub)

    return Handler


# --- entry point ------------------------------------------------------------

def build(game_path: str, *, admin_token: str | None = None) -> GameServer:
    with open(game_path, encoding="utf-8") as fh:
        game = Game.from_json(fh.read())
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    game_dir = os.path.join(here, "games", game.game_id)
    app = GameServer(
        Engine(game),
        Store(os.path.join(game_dir, "events.jsonl")),
        static_dir=os.path.join(here, "static"),
        media_dir=os.path.join(game_dir, "media"),
        admin_token=admin_token or secrets.token_urlsafe(9),
    )
    app.resume()
    return app


def announce(app: GameServer, host: str, port: int) -> None:
    print(f"game:    {app.engine.game.title}")
    print(f"join:    http://{lan_host(host)}:{port}/")
    print(f"token:   {app.admin_token}   (host seat; not a spoiler channel)")
    print(f"journal: {app.store.path}")


def run(game_path: str, host: str, port: int) -> None:
    """Boot the server and print the host admin token to the console."""
    app = build(game_path)
    announce(app, host, port)
    try:
        app.serve_forever(host, port)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


def lan_host(host: str) -> str:
    """A wildcard bind is not an address anyone can type into a browser."""
    if host not in ("0.0.0.0", "", "::"):
        return host
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))   # no packets sent; just picks a route
        return probe.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        probe.close()
