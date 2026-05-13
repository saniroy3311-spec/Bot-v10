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
"""
from __future__ import annotations

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

PORT          = int(os.environ.get("DASHBOARD_PORT", "10000"))
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Shared state (set by main.py before server starts) ────────────────────────
_journal: "Journal | None" = None
_bot_live: bool            = False

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

    def do_GET(self):
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


# ── Public start function ──────────────────────────────────────────────────────

def start() -> None:
    """
    Launch the HTTP server in a daemon thread.
    Call AFTER init(journal) so the journal is wired before any request arrives.
    """
    httpd = HTTPServer(("0.0.0.0", PORT), _Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="dashboard-server")
    t.start()
    logger.info(f"Dashboard LIVE → http://0.0.0.0:{PORT}")
