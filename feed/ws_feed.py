"""
feed/ws_feed.py
OHLCV feed — WebSocket PRIMARY, REST FALLBACK.

WHY THE 1-MINUTE DELAY EXISTED:
══════════════════════════════════════════════════════════════════════════════
The REST OHLCV endpoint on Delta India has a ~60s server-side ingestion lag.
The old code polled every 2s, but the endpoint simply did not return the new
bar until ~60s after it opened. No amount of faster polling could fix this.

Example from live logs:
  Bar that opened  at 13:50:00 IST (new_ts=1775118000000)
  First detected   at 13:51:06 IST → 66 seconds late
  TV fired signal  at 13:50:00 IST → missed window

THE FIX — WebSocket candle subscription:
══════════════════════════════════════════════════════════════════════════════
Delta India pushes candlestick updates via WebSocket every tick. When a new
candle starts (timestamp changes), we know the previous bar JUST closed.
Detection latency: 1–3 seconds vs 60+ seconds with REST.

Architecture:
  1. STARTUP : Load 260 historical bars via REST (unchanged).
  2. LIVE    : Subscribe to candlestick_{timeframe} on Delta India WS.
               Update rolling df from incoming candle updates.
               When candle timestamp changes → bar closed → fire signal.
  3. FALLBACK: If WS fails > MAX_WS_FAILURES consecutive reconnects,
               fall back to REST polling (old behaviour, 60s lag).

Delta India WebSocket details:
  URL      : wss://socket.india.delta.exchange
  Subscribe: {"type":"subscribe","payload":{"channels":[{"name":"candlestick_5m","symbols":["BTCUSD"]}]}}
  Heartbeat: send {"type":"heartbeat"} every 30s (server drops idle connections)
  Symbol   : "BTCUSD"  (NOT the ccxt "BTC/USD:USD" format)
  Timeframe: "candlestick_5m" / "candlestick_30m" etc.

PRESERVED FIXES from previous version:
  - Pass confirmed-bars-only slice to on_bar_close (no live bar in df)
  - Bar-exact parity: df.iloc[-1] = the bar Pine just fired on
  - REST historical load on startup
  - MIN_BARS guard (need EMA_TREND_LEN + 10 bars before signals)
"""

import asyncio
import json
import logging
import time
from typing import Optional

import pandas as pd
import ccxt
import websockets
import websockets.exceptions

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, CANDLE_TIMEFRAME, WS_RECONNECT_SEC, EMA_TREND_LEN,
)

logger   = logging.getLogger(__name__)
MIN_BARS = EMA_TREND_LEN + 10   # 210 for EMA-200

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

_WS_LIVE    = "wss://socket.india.delta.exchange"
_WS_TESTNET = "wss://testnet-socket.india.delta.exchange"

# How many consecutive WS failures before we give up and use REST fallback
_MAX_WS_FAILURES = 5
# WebSocket heartbeat interval (seconds) — server drops idle connections
_WS_HEARTBEAT_SEC = 30


def _ccxt_to_ws_symbol(ccxt_symbol: str) -> str:
    """
    Convert ccxt unified symbol → Delta India WS product symbol.
    "BTC/USD:USD"  →  "BTCUSD"
    "ETH/USD:USD"  →  "ETHUSD"
    """
    return ccxt_symbol.split(":")[0].replace("/", "")


def _timeframe_to_channel(timeframe: str) -> str:
    """
    "5m"  → "candlestick_5m"
    "30m" → "candlestick_30m"
    "1h"  → "candlestick_1h"
    """
    return f"candlestick_{timeframe}"


def _ts_to_ms(ts) -> int:
    """
    Normalise Delta WS timestamp to milliseconds.
    Delta India WS sends candle timestamps in SECONDS (Unix epoch).
    REST OHLCV returns milliseconds. We store everything in ms internally.
    """
    ts = int(ts)
    # If it's already in milliseconds (13 digits), leave it alone.
    # If it's in seconds (10 digits), multiply by 1000.
    if ts < 1_000_000_000_000:
        return ts * 1000
    return ts


class CandleFeed:
    def __init__(self, on_bar_close, on_feed_ready=None):
        self.on_bar_close  = on_bar_close
        async def _noop(): pass
        self.on_feed_ready = on_feed_ready or _noop
        self._last_bar_ts  = 0   # ms
        self._df           = pd.DataFrame()
        self._exchange     = None
        self._ready_fired  = False
        self._ws_failures  = 0

    # ── Public entry point ────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Load historical bars, then run the feed loop.
        Primary: WebSocket. Fallback: REST polling.
        """
        await self._load_history()

        if not self._ready_fired:
            self._ready_fired = True
            await self.on_feed_ready()

        while True:
            if self._ws_failures < _MAX_WS_FAILURES:
                try:
                    await self._run_websocket()
                except Exception as e:
                    self._ws_failures += 1
                    logger.error(
                        f"WebSocket feed error (failure {self._ws_failures}/"
                        f"{_MAX_WS_FAILURES}): {e}"
                    )
                    if self._ws_failures < _MAX_WS_FAILURES:
                        wait = min(WS_RECONNECT_SEC * (2 ** (self._ws_failures - 1)), 60)
                        logger.info(f"Reconnecting in {wait}s...")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(
                            f"WebSocket failed {_MAX_WS_FAILURES} times — "
                            f"switching to REST polling fallback."
                        )
            else:
                # REST fallback — old behaviour, ~60s lag
                try:
                    await self._poll_rest()
                except Exception as e:
                    logger.error(f"REST poll error: {e}", exc_info=True)
                    await asyncio.sleep(WS_RECONNECT_SEC)

    # ── Historical load (REST) ────────────────────────────────────────────────

    async def _load_history(self) -> None:
        base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        }
        self._exchange = ccxt.delta(params)
        logger.info(f"Loading market map from Delta India ({base_url})...")
        self._exchange.load_markets()

        if SYMBOL not in self._exchange.markets:
            available = [
                s for s in self._exchange.markets
                if "BTC" in s and "USD" in s and ":" in s and len(s) < 15
            ]
            raise ValueError(
                f"SYMBOL '{SYMBOL}' not found on Delta India.\n"
                f"Available BTC perpetuals: {available}\n"
                f"Fix: update SYMBOL= in your .env"
            )
        logger.info(f"Symbol {SYMBOL} verified ✅")

        fetch_limit = MIN_BARS + 50
        logger.info(
            f"Loading {fetch_limit} historical bars via REST "
            f"for [{SYMBOL}] [{CANDLE_TIMEFRAME}]..."
        )
        ohlcv    = self._exchange.fetch_ohlcv(SYMBOL, CANDLE_TIMEFRAME, limit=fetch_limit)
        self._df = self._to_df(ohlcv)

        # _last_bar_ts = open timestamp of the current live (not-yet-closed) bar
        self._last_bar_ts = int(self._df.iloc[-1]["timestamp"])

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️'})"
        )

    # ── WebSocket feed (PRIMARY — <2s delay) ──────────────────────────────────

    async def _run_websocket(self) -> None:
        ws_url     = _WS_TESTNET if DELTA_TESTNET else _WS_LIVE
        ws_symbol  = _ccxt_to_ws_symbol(SYMBOL)      # "BTCUSD"
        channel    = _timeframe_to_channel(CANDLE_TIMEFRAME)  # "candlestick_5m"

        subscribe_msg = json.dumps({
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": channel, "symbols": [ws_symbol]}
                ]
            }
        })
        heartbeat_msg = json.dumps({"type": "heartbeat"})

        logger.info(
            f"WebSocket connecting → {ws_url} | "
            f"channel={channel} symbol={ws_symbol}"
        )

        async with websockets.connect(
            ws_url,
            ping_interval=None,      # we handle heartbeat manually
            close_timeout=10,
        ) as ws:
            await ws.send(subscribe_msg)
            logger.info(
                f"WebSocket subscribed ✅ | "
                f"bar-close detection active (replaces 60s REST lag)"
            )
            self._ws_failures = 0    # reset failure counter on successful connect

            last_heartbeat = time.time()

            async for raw in ws:
                # ── Heartbeat ─────────────────────────────────────────────────
                now = time.time()
                if now - last_heartbeat >= _WS_HEARTBEAT_SEC:
                    await ws.send(heartbeat_msg)
                    last_heartbeat = now

                # ── Parse message ─────────────────────────────────────────────
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # Ignore subscription ack, heartbeat replies, etc.
                if msg_type not in (channel, f"candlestick_{CANDLE_TIMEFRAME}"):
                    continue

                data = msg.get("data") or msg
                if not data:
                    continue

                await self._process_ws_candle(data)

    async def _process_ws_candle(self, data: dict) -> None:
        """
        Process one candlestick message from the WebSocket.

        Delta India pushes an update every tick for the CURRENT live bar.
        When the candle timestamp changes (new bar opened), the previous
        bar just closed — exactly when Pine fires its signal.

        Timing vs TV:
          TV  : fires at bar close T+0s
          Bot : detects at T+1–3s (next WS candle push after new bar opens)
          Old : detected at T+60s (REST lag)
        """
        # Extract candle open timestamp — try every field name Delta uses
        raw_ts = (
            data.get("timestamp") or
            data.get("start")     or
            data.get("time")      or
            data.get("candle_start_time") or
            0
        )
        if not raw_ts:
            return

        candle_ts_ms = _ts_to_ms(raw_ts)

        # Extract OHLCV
        try:
            o = float(data.get("open",   0))
            h = float(data.get("high",   0))
            l = float(data.get("low",    0))
            c = float(data.get("close",  0))
            v = float(data.get("volume", 0))
        except (TypeError, ValueError):
            return

        if c <= 0:
            return

        if candle_ts_ms > self._last_bar_ts:
            # ── NEW BAR: the previous live bar just closed ─────────────────
            # df.iloc[-1] is that closed bar (we update it with final values below).
            # Fire the signal with confirmed-bars-only slice.
            closed_ts = self._last_bar_ts
            new_ts    = candle_ts_ms
            logger.info(
                f"✅ Bar confirmed [WS] | closed_ts={closed_ts} | "
                f"new_ts={new_ts} | bars={len(self._df)} — evaluating signals..."
            )

            # Finalise the closing bar with last WS values before firing
            if not self._df.empty:
                idx = self._df.index[-1]
                self._df.at[idx, "open"]   = o
                self._df.at[idx, "high"]   = h
                self._df.at[idx, "low"]    = l
                self._df.at[idx, "close"]  = c
                self._df.at[idx, "volume"] = v

            if len(self._df) >= MIN_BARS:
                await self.on_bar_close(self._df.copy())
            else:
                logger.warning(
                    f"⚠️ Bar skipped — only {len(self._df)} bars (need {MIN_BARS})."
                )

            # Append new live bar to rolling buffer
            new_row = pd.DataFrame([{
                "timestamp": candle_ts_ms,
                "open" : o, "high": h, "low": l, "close": c, "volume": v,
            }])
            self._df = pd.concat(
                [self._df, new_row], ignore_index=True
            ).tail(MIN_BARS + 50)
            self._last_bar_ts = candle_ts_ms

        else:
            # ── SAME BAR: update live candle in-place ─────────────────────
            if not self._df.empty:
                idx = self._df.index[-1]
                self._df.at[idx, "open"]   = o
                self._df.at[idx, "high"]   = h
                self._df.at[idx, "low"]    = l
                self._df.at[idx, "close"]  = c
                self._df.at[idx, "volume"] = v

    # ── REST polling fallback (used only if WS fails 5 times) ────────────────

    async def _poll_rest(self) -> None:
        """
        Original REST polling — ~60s lag. Only used as last-resort fallback
        if WebSocket fails repeatedly. Identical to previous ws_feed.py.
        """
        sleep_sec = 2
        logger.warning(
            f"REST fallback active — polling every {sleep_sec}s "
            f"[~60s bar detection lag]. Fix WS connection to restore speed."
        )

        while True:
            await asyncio.sleep(sleep_sec)
            try:
                ohlcv = self._exchange.fetch_ohlcv(
                    SYMBOL, CANDLE_TIMEFRAME, limit=5
                )
                if not ohlcv:
                    continue

                latest    = ohlcv[-1]
                latest_ts = int(latest[0])

                if latest_ts > self._last_bar_ts:
                    if len(self._df) >= MIN_BARS:
                        closed_ts = self._last_bar_ts
                        logger.info(
                            f"✅ Bar confirmed [REST fallback] | "
                            f"closed_ts={closed_ts} | new_ts={latest_ts} | "
                            f"bars={len(self._df)} — evaluating signals..."
                        )
                        await self.on_bar_close(self._df.copy())
                    else:
                        logger.warning(
                            f"⚠️ Bar skipped — only {len(self._df)} bars "
                            f"(need {MIN_BARS})."
                        )

                    new_row = pd.DataFrame([{
                        "timestamp": latest_ts,
                        "open"  : float(latest[1]),
                        "high"  : float(latest[2]),
                        "low"   : float(latest[3]),
                        "close" : float(latest[4]),
                        "volume": float(latest[5]),
                    }])
                    self._df = pd.concat(
                        [self._df, new_row], ignore_index=True
                    ).tail(MIN_BARS + 50)
                    self._last_bar_ts = latest_ts

                else:
                    if not self._df.empty:
                        idx = self._df.index[-1]
                        self._df.at[idx, "open"]   = float(latest[1])
                        self._df.at[idx, "high"]   = float(latest[2])
                        self._df.at[idx, "low"]    = float(latest[3])
                        self._df.at[idx, "close"]  = float(latest[4])
                        self._df.at[idx, "volume"] = float(latest[5])

            except ccxt.NetworkError as e:
                logger.warning(f"REST network error: {e} — retrying...")
                await asyncio.sleep(WS_RECONNECT_SEC)
            except Exception as e:
                logger.error(f"REST poll error: {e}", exc_info=True)
                break

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_df(ohlcv: list) -> pd.DataFrame:
        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        return df.astype({
            "open": float, "high": float,
            "low": float, "close": float, "volume": float,
        })
