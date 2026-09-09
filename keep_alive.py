"""
keep_alive.py — Render / UptimeRobot health-check server.

Why this exists
---------------
Render requires a process to bind the PORT env-var immediately or it
marks the deploy as failed (HTTP 502 Bad Gateway).  UptimeRobot (and
Render's own health checks) probe with HEAD requests by default.
Python's BaseHTTPRequestHandler returns 501 Not Implemented for any
verb that has no handler — so both GET *and* HEAD must be handled.

Key design decision — daemon=False
-----------------------------------
The health thread is intentionally NOT a daemon thread.  Daemon threads
die the instant the main thread exits.  If the Telegram bot crashes
(network error, bad token, Playwright failure, etc.) but the health
server keeps returning 200, UptimeRobot won't fire a false alarm and
Render won't immediately kill + restart the process mid-recovery.
The __main__ block in air.py wraps main() in a restart loop, so the
bot recovers on its own without the process needing to die.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    """Minimal handler — 200 OK for GET and HEAD, silence the access log."""

    def _send_ok(self) -> None:
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return body

    def do_GET(self) -> None:          # curl / browser check
        self.wfile.write(self._send_ok())

    def do_HEAD(self) -> None:         # UptimeRobot & Render default probe
        self._send_ok()                # headers only — no body for HEAD

    def log_message(self, *_) -> None: # suppress noisy access logs
        pass


def start(port: int | None = None) -> ThreadingHTTPServer:
    """Bind the health-check port and start serving in a background thread.

    Call this as the very first thing in __main__ so PORT is bound before
    any slow initialisation (Firebase, Playwright, Telegram handshake…).

    Returns the server instance in case the caller wants to shut it down.
    """
    if port is None:
        port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="keep-alive",
        daemon=False,   # <-- stays alive even if the bot thread crashes
    )
    thread.start()
    print(f"🌐 Health server bound on 0.0.0.0:{port} (GET + HEAD → 200 OK)")
    return server
