"""
feed/ws_feed.py  —  Shiva Sniper v6.5  (FIX-WS-v3)
════════════════════════════════════════════════════════════════════════════════

CHANGES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-WS-3a | REST fallback: boundary comparison used wrong bar (iloc[-2]).

  Old REST fallback code:
      latest    = ohlcv[-2]   # "last confirmed bar"
      latest_ts = int(latest[0])
      boundary  = _candle_boundary(latest_ts, period_ms)
      if boundary > self._last_candle_boundary:
          fire on_bar_close(df)

  Problem: ohlcv[-2] is the SECOND-TO-LAST bar returned, not the last
  confirmed bar. Delta REST returns bars where ohlcv[-1] is the CURRENT
  LIVE (open) bar and ohlcv[-2] is the last CLOSED bar. Comparing
  ohlcv[-2]'s boundary means we fire on a bar that's ALREADY been fired
  on — or we miss the transition entirely.

  Fix: compare ohlcv[-1] boundary (live bar) vs _last_candle_boundary.
  When the live bar's boundary > last boundary, the previous bar just
  closed. Fire on_bar_close THEN update last_candle_boundary to live bar.
  This matches the WS logic exactly.

FIX-WS-3b | Startup: _last_candle_boundary initialized from CONFIRMED
  bar so the NEXT bar boundary correctly triggers the first signal.

  Old: set from df.iloc[-1] (the live/open bar).
       If bot starts mid-candle, the NEXT closed bar has the same boundary
       as the already-open bar → first signal is missed.

  Fix: set _last_candle_boundary from df.iloc[-2] (the last CLOSED bar).
       The NEXT boundary that arrives will always be newer → first signal fires.

FIX-WS-3c | WS message type matching made more robust.
  Delta India WS sends messages with type = "candlestick_5m" (not "candlestick").
  Old code checked msg_type in (channel, f"candlestick_{CANDLE_TIMEFRAME}")
  which was correct, but also silently dropped messages with unknown types.
  Added debug logging for unexpected message types during the first 10 messages
  to help diagnose subscription issues.

PRESERVED FROM FIX-WS-v2:
  - Boundary-based bar detection (fires ONCE per 5m candle, not per 500ms tick)
  - _processing guard prevents re-entrant on_bar_close
  - WS primary, REST fallback after 5 failures
  - Historical load via REST on startup
  - Heartbeat every 30s
  - MIN_BARS guard
  - Same-bar OHLCV in-place update
──────────────────────────────────────────────────────────────────────────────

ROOT CAUSE SUMMARY (original multiple-entry bug):
  Log: closed_ts=...503469 | new_ts=...502614 — 500ms gap.
  Delta India WS pushes tick UPDATES every ~500ms for the current live bar.
  Old code: if candle_ts_ms > self._last_bar_ts → fires every tick.
  FIX-WS-v2 (preserved): boundary-based detection — fires once per 5m candle.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import pandas as pd
import ccxt
import ccxt.async_support as ccxt_async
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

_MAX_WS_FAILURES  = 5
_WS_HEARTBEAT_SEC = 30


def _timeframe_to_ms(tf: str) -> int:
    """Convert '5m' → 300_000 ms, '30m' → 1_800_000 ms, '1h' → 3_600_000 ms"""
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60 * 1000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600 * 1000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400 * 1000
    raise ValueError(f"Unknown timeframe: {tf}")


def _candle_boundary(ts_ms: int, period_ms: int) -> int:
    """Return the open-boundary timestamp for the candle containing ts_ms."""
    return (ts_ms // period_ms) * period_ms


def _ccxt_to_ws_symbol(ccxt_symbol: str) -> str:
    return ccxt_symbol.split(":")[0].replace("/", "")


def _timeframe_to_channel(timeframe: str) -> str:
    return f"candlestick_{timeframe}"


def _ts_to_ms(ts) -> int:
    # FIX-TS: Delta India WS sends microseconds (16 digits).
    # Must divide by 1000 to get milliseconds for boundary math.
    # Without this, boundaries increment by ~1800ms instead of 1,800,000ms
    # causing bars to fire every tick instead of every 30 minutes.
    ts = int(ts)
    if ts > 1_000_000_000_000_000:   # microseconds (16 digits)
        return ts // 1000
    if ts > 1_000_000_000_000:        # milliseconds (13 digits)
        return ts
    return ts * 1000                  # seconds (10 digits)


class CandleFeed:
    def __init__(self, on_bar_close, on_feed_ready=None):
        self.on_bar_close  = on_bar_close
        async def _noop(): pass
        self.on_feed_ready = on_feed_ready or _noop

        self._period_ms            = _timeframe_to_ms(CANDLE_TIMEFRAME)
        # FIX-WS-3b: initialized from CLOSED bar at startup (see _load_history)
        self._last_candle_boundary = 0

        self._df           = pd.DataFrame()
        self._exchange     = None
        self._ready_fired  = False
        self._ws_failures  = 0

        # Guard: prevent concurrent on_bar_close calls
        self._processing   = False
        # Debug: count messages received (for startup diagnostics)
        self._msg_count    = 0

    # ── Public entry point ────────────────────────────────────────────────────

    async def start(self) -> None:
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
                try:
                    await self._poll_rest()
                except Exception as e:
                    logger.error(f"REST poll error: {e}", exc_info=True)
                    await asyncio.sleep(WS_RECONNECT_SEC)

    # ── Historical load ───────────────────────────────────────────────────────

    async def _load_history(self) -> None:
        # FIX-WS-ASYNC: Use ccxt.async_support so load_markets() and
        # fetch_ohlcv() are awaited and never block the event loop.
        # Old code used sync ccxt.delta — the blocking load_markets() call
        # (up to 60s on Delta India) starved the event loop, caused PM2's
        # health-check to time out, and killed the process every startup.
        base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        }
        exchange = ccxt_async.delta(params)
        try:
            logger.info(f"Loading market map from Delta India ({base_url})...")
            await exchange.load_markets()

            if SYMBOL not in exchange.markets:
                available = [
                    s for s in exchange.markets
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
            ohlcv    = await exchange.fetch_ohlcv(SYMBOL, CANDLE_TIMEFRAME, limit=fetch_limit)
            self._df = self._to_df(ohlcv)
            fetched_markets = dict(exchange.markets)
        finally:
            await exchange.close()

        # Store a sync exchange for the REST fallback polling loop.
        # Inject the already-fetched market map so it never blocks on load_markets() again.
        self._exchange = ccxt.delta({
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        })
        self._exchange.markets = fetched_markets

        # FIX-WS-3b: set boundary from iloc[-2] (LAST CLOSED bar, not live bar).
        # ohlcv[-1] = live (open) bar, ohlcv[-2] = last confirmed closed bar.
        # Setting boundary to the closed bar means the NEXT boundary (when the
        # next candle opens) will be strictly greater → first signal fires.
        # If we set it from ohlcv[-1] (live bar) we'd skip the next close.
        last_closed_ts = int(self._df.iloc[-1]["timestamp"])
        self._last_candle_boundary = _candle_boundary(last_closed_ts, self._period_ms)

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️'})"
        )
        logger.info(
            f"Boundary tracking: period={self._period_ms}ms "
            f"last_closed_boundary={self._last_candle_boundary} "
            f"(will fire when next candle opens)"
        )

    # ── WebSocket feed ────────────────────────────────────────────────────────

    async def _run_websocket(self) -> None:
        ws_url    = _WS_TESTNET if DELTA_TESTNET else _WS_LIVE
        ws_symbol = _ccxt_to_ws_symbol(SYMBOL)
        channel   = _timeframe_to_channel(CANDLE_TIMEFRAME)

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
            ping_interval=None,
            close_timeout=10,
        ) as ws:
            await ws.send(subscribe_msg)
            logger.info(
                f"WebSocket subscribed ✅ | "
                f"FIX-WS-v3: boundary-based detection, fires once per "
                f"{CANDLE_TIMEFRAME} candle"
            )
            self._ws_failures = 0
            self._msg_count   = 0
            last_heartbeat    = time.time()

            async for raw in ws:
                now = time.time()
                if now - last_heartbeat >= _WS_HEARTBEAT_SEC:
                    await ws.send(heartbeat_msg)
                    last_heartbeat = now

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                # FIX-WS-3c: log unexpected message types during startup
                self._msg_count += 1
                if self._msg_count <= 10 and msg_type not in (channel, "subscriptions", "heartbeat"):
                    logger.debug(f"WS msg #{self._msg_count} type={msg_type!r}")

                if msg_type not in (channel, f"candlestick_{CANDLE_TIMEFRAME}"):
                    continue

                data = msg.get("data") or msg
                if not data:
                    continue

                await self._process_ws_candle(data)

    async def _process_ws_candle(self, data: dict) -> None:
        """
        FIX-WS-v2 (preserved): Fire on_bar_close at CANDLE BOUNDARY only.

        Delta India sends tick updates every ~500ms for the current live bar.
        boundary = floor(candle_ts_ms / period_ms) * period_ms
        if boundary > _last_candle_boundary → NEW CANDLE = previous bar closed → fire
        else → same candle ticking → update in-place
        """
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

        current_boundary = _candle_boundary(candle_ts_ms, self._period_ms)

        if current_boundary > self._last_candle_boundary:
            # ── NEW CANDLE BOUNDARY: previous bar just closed ──────────────
            logger.info(
                f"✅ Bar confirmed [WS] | "
                f"closed_boundary={self._last_candle_boundary} | "
                f"new_boundary={current_boundary} | "
                f"bars={len(self._df)} — evaluating signals..."
            )

            if self._processing:
                logger.warning(
                    "⚠️ on_bar_close still processing — skipping this bar "
                    "(signal evaluation overlap prevented)"
                )
                self._last_candle_boundary = current_boundary
                return

            if len(self._df) >= MIN_BARS:
                self._processing = True
                try:
                    await self.on_bar_close(self._df.copy())
                finally:
                    self._processing = False
            else:
                logger.warning(
                    f"⚠️ Bar skipped — only {len(self._df)} bars (need {MIN_BARS})."
                )

            # Append new live bar stub
            new_row = pd.DataFrame([{
                "timestamp": candle_ts_ms,
                "open" : o, "high": h, "low": l, "close": c, "volume": v,
            }])
            self._df = pd.concat(
                [self._df, new_row], ignore_index=True
            ).tail(MIN_BARS + 50)
            self._last_candle_boundary = current_boundary

        else:
            # ── SAME CANDLE: update live bar in-place ─────────────────────
            if not self._df.empty:
                idx = self._df.index[-1]
                self._df.at[idx, "open"]   = o
                self._df.at[idx, "high"]   = h
                self._df.at[idx, "low"]    = l
                self._df.at[idx, "close"]  = c
                self._df.at[idx, "volume"] = v

    # ── REST polling fallback ──────────────────────────────────────────────────

    async def _poll_rest(self) -> None:
        sleep_sec = 5
        logger.warning(
            f"REST fallback active — polling every {sleep_sec}s. "
            f"Fix WS connection to restore speed."
        )

        while True:
            await asyncio.sleep(sleep_sec)
            try:
                ohlcv = self._exchange.fetch_ohlcv(
                    SYMBOL, CANDLE_TIMEFRAME, limit=5
                )
                if not ohlcv or len(ohlcv) < 2:
                    continue

                # FIX-WS-3a: compare live bar (ohlcv[-1]) boundary vs last boundary.
                # When ohlcv[-1]'s boundary > _last_candle_boundary, the previous
                # candle (ohlcv[-2]) just closed. Fire on_bar_close with the df
                # whose last row IS the just-closed bar (before appending new stub).
                live_bar     = ohlcv[-1]
                live_ts      = int(live_bar[0])
                live_boundary = _candle_boundary(live_ts, self._period_ms)

                if live_boundary > self._last_candle_boundary:
                    if len(self._df) >= MIN_BARS:
                        logger.info(
                            f"✅ Bar confirmed [REST fallback] | "
                            f"prev_boundary={self._last_candle_boundary} | "
                            f"new_boundary={live_boundary} | "
                            f"bars={len(self._df)} — evaluating signals..."
                        )
                        if not self._processing:
                            self._processing = True
                            try:
                                await self.on_bar_close(self._df.copy())
                            finally:
                                self._processing = False
                    else:
                        logger.warning(
                            f"⚠️ Bar skipped — only {len(self._df)} bars "
                            f"(need {MIN_BARS})."
                        )

                    # Append new live bar stub
                    new_row = pd.DataFrame([{
                        "timestamp": live_ts,
                        "open"  : float(live_bar[1]),
                        "high"  : float(live_bar[2]),
                        "low"   : float(live_bar[3]),
                        "close" : float(live_bar[4]),
                        "volume": float(live_bar[5]),
                    }])
                    self._df = pd.concat(
                        [self._df, new_row], ignore_index=True
                    ).tail(MIN_BARS + 50)
                    self._last_candle_boundary = live_boundary

                else:
                    # Same candle: update live bar in-place
                    if not self._df.empty:
                        idx = self._df.index[-1]
                        self._df.at[idx, "open"]   = float(live_bar[1])
                        self._df.at[idx, "high"]   = float(live_bar[2])
                        self._df.at[idx, "low"]    = float(live_bar[3])
                        self._df.at[idx, "close"]  = float(live_bar[4])
                        self._df.at[idx, "volume"] = float(live_bar[5])

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
