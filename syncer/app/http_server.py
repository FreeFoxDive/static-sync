"""Read-only health and status HTTP server."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.status import StatusStore

log = logging.getLogger(__name__)


def serve(bind: str, port: int, status: StatusStore) -> ThreadingHTTPServer:
    store = status

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._json(200, {"ok": True})
                return
            if path == "/status":
                self._json(200, store.snapshot())
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server
