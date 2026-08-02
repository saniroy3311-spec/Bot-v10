"""
server.py — Shiva Sniper v10  Dashboard HTTP Server
═══════════════════════════════════════════════════════════════════════════════

Serves dashboard.html and all /api/* endpoints the dashboard polls every 5s.

Endpoints
─────────
  GET /                        → dashboard.html (static)
  GET /api/status              → {"status": "live"} when bot is running
  GET /api/summary             → Journal.get_summary()
  GET /api/trades?limit=50     → Journal.get_trades(limit)
  GET /api/position            → Journal.get_open_trade() or {}
  GET /api/candles?limit=200   → Binance 30m OHLCV via ccxt

Running
───────
  Started automatically from main.py — no manual launch needed.
  Accessible at http://<vps-ip>:10000

  PORT can be changed via .env:  DASHBOARD_PORT=10000
  HOST can be changed via .env:  DASHBOARD_HOST=0.0.0.0   (use 127.0.0.1 to
                                 keep the dashboard private + SSH-tunnel in)
"""
from __future__ import annotations

import base64
import errno
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import ccxt

if TYPE_CHECKING:
    from infra.journal import Journal

logger = logging.getLogger(__name__)

PORT          = int(os.environ.get("DASHBOARD_PORT", "80"))
HOST          = "0.0.0.0"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

# How long start() will keep retrying the port bind before giving up.
# This rides out the brief window where an old instance is still shutting
# down and hasn't released the port yet (the usual cause of the
# "Address already in use" crash loop on pm2 restart).
_BIND_RETRY_SECONDS = float(os.environ.get("DASHBOARD_BIND_RETRY", "20"))

# ── Basic Auth credentials (set in .env or change defaults here) ──────────────
DASH_USER = os.environ.get("DASHBOARD_USER", "shiva")
DASH_PASS = os.environ.get("DASHBOARD_PASS", "sniper123")
_AUTH_TOKEN = base64.b64encode(f"{DASH_USER}:{DASH_PASS}".encode()).decode()

# ── Shared state (set by main.py before server starts) ────────────────────────
_journal: "Journal | None" = None
_bot_live: bool            = False
_httpd:   "HTTPServer | None" = None   # kept so stop() can shut it down cleanly

# ── Candle cache — refresh every 5 min to avoid hammering Binance REST ────────
_candle_cache:      list  = []
_candle_cache_ts:   float = 0.0
_CANDLE_CACHE_TTL:  float = 300.0   # 5 minutes


def init(journal: "Journal") -> None:
    """Call from main.py after Journal is created, before start()."""
    global _journal, _bot_live
    _journal  = journal
    _bot_live = True


def set_live(live: bool) -> None:
    global _bot_live
    _bot_live = live


# ── Binance candle fetch ───────────────────────────────────────────────────────

def _fetch_candles_binance(limit: int = 200) -> list:
    """
    Fetch BTC/USDT 30m candles from Binance REST (no API key needed).
    Returns [{time, open, high, low, close}] suitable for Lightweight Charts.
    """
    global _candle_cache, _candle_cache_ts

    now = time.monotonic()
    if _candle_cache and (now - _candle_cache_ts) < _CANDLE_CACHE_TTL:
        return _candle_cache[-limit:]

    try:
        ex    = ccxt.binance({"enableRateLimit": True})
        ohlcv = ex.fetch_ohlcv("BTC/USDT", "30m", limit=limit)
        candles = [
            {
                "time":  bar[0] // 1000,   # ms → Unix seconds for LWC
                "open":  bar[1],
                "high":  bar[2],
                "low":   bar[3],
                "close": bar[4],
            }
            for bar in ohlcv
        ]
        _candle_cache    = candles
        _candle_cache_ts = now
        logger.debug(f"[SERVER] Candles refreshed — {len(candles)} bars")
        return candles[-limit:]
    except Exception as e:
        logger.warning(f"[SERVER] Binance candle fetch failed: {e}")
        return _candle_cache[-limit:] if _candle_cache else []


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Silence default access log spam — bot logs are noisy enough
        pass

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, mime: str) -> None:
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",   mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "File not found")

    def _check_auth(self) -> bool:
        """Return True if request has valid Basic Auth credentials."""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            token = auth_header[6:]
            if token == _AUTH_TOKEN:
                return True
        # Send 401 — browser will show login popup
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Shiva Sniper Dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        # auth disabled
        pass
        if False:
            return

        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        # ── Static dashboard ──────────────────────────────────────────────────
        if path in ("/", "/dashboard", "/dashboard.html"):
            self._send_file(
                os.path.join(DASHBOARD_DIR, "dashboard.html"),
                "text/html; charset=utf-8",
            )
            return

        # ── API routes ────────────────────────────────────────────────────────
        if path == "/api/status":
            self._send_json({"status": "live" if _bot_live else "offline"})

        elif path == "/api/summary":
            data = _journal.get_summary() if _journal else {}
            self._send_json(data)

        elif path == "/api/trades":
            limit = int(params.get("limit", ["50"])[0])
            data  = _journal.get_trades(limit=limit) if _journal else []
            self._send_json(data)

        elif path == "/api/position":
            data = _journal.get_open_trade() if _journal else None
            self._send_json(data or {})

        elif path == "/api/candles":
            limit   = int(params.get("limit", ["200"])[0])
            candles = _fetch_candles_binance(limit)
            self._send_json(candles)

        else:
            self._send_json({"error": "not found"}, 404)


# ── Bind helper — rides out a slow-dying previous instance ─────────────────────

def _bind_with_retry() -> HTTPServer:
    """
    Try to bind the dashboard port. If the port is still held (errno 98,
    EADDRINUSE) — almost always an old instance that hasn't finished
    shutting down yet — wait and retry for up to _BIND_RETRY_SECONDS
    instead of crashing the whole bot.

    HTTPServer already sets SO_REUSEADDR, so a socket merely lingering in
    TIME_WAIT would NOT raise EADDRINUSE. If we still hit it, a live
    process is actively listening on the port. Retrying gives a sibling
    that is mid-shutdown time to exit. We deliberately do NOT use
    SO_REUSEPORT: that would let two bot instances run at once and place
    duplicate orders — far worse than a failed bind.
    """
    deadline = time.monotonic() + _BIND_RETRY_SECONDS
    attempt  = 0
    while True:
        attempt += 1
        try:
            return HTTPServer((HOST, PORT), _Handler)
        except OSError as e:
            if e.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                # Either a different error, or we've waited long enough.
                raise
            logger.warning(
                f"[SERVER] Port {PORT} busy (attempt {attempt}) — an old "
                f"instance is probably still shutting down. Retrying in 2s..."
            )
            time.sleep(2)


# ── Public start / stop functions ──────────────────────────────────────────────

def start() -> None:
    """
    Launch the HTTP server in a daemon thread.
    Call AFTER init(journal) so the journal is wired before any request arrives.
    """
    global _httpd

    # Warn loudly if the shipped default password is still in use on a
    # publicly reachable bind. Anyone scanning the VPS can otherwise log in.
    if DASH_PASS == "sniper123" and HOST not in ("127.0.0.1", "localhost"):
        logger.warning(
            "[SERVER] ⚠ Dashboard is using the DEFAULT password on a public "
            "bind. Set DASHBOARD_PASS in your .env (and consider "
            "DASHBOARD_HOST=127.0.0.1 + an SSH tunnel)."
        )

    _httpd = _bind_with_retry()
    _httpd.daemon_threads = True
    t = threading.Thread(target=_httpd.serve_forever, daemon=True, name="dashboard-server")
    t.start()
    logger.info(f"Dashboard LIVE → http://{HOST}:{PORT}")


def stop() -> None:
    """
    Stop the HTTP server and release the port immediately.
    Call this EARLY in the bot's shutdown() so the port is free well
    before pm2 starts the replacement process.
    """
    global _httpd
    if _httpd is None:
        return
    try:
        _httpd.shutdown()       # unblock serve_forever()
        _httpd.server_close()   # close the listening socket → frees the port
        logger.info("[SERVER] Dashboard stopped, port released.")
    except Exception as e:
        logger.warning(f"[SERVER] Error during dashboard stop: {e}")
    finally:
        _httpd = None
