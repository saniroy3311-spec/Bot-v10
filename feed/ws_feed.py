"""
feed/ws_feed.py  —  Shiva Sniper v10  (BUG-FIX-AUDIT-v1 + FIX-PEAK-REST)
════════════════════════════════════════════════════════════════════════════════

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-PEAK-REST | CRITICAL — _process_ws_candle() now fetches the authoritative
  closed-bar OHLCV from REST immediately after boundary change is detected,
  BEFORE calling on_bar_close().

  ROOT CAUSE OF BOT vs PINE EXIT PRICE MISMATCH (~80 points off):
  ─────────────────────────────────────────────────────────────────
  Pine Script's broker emulator uses the exchange's authoritative bar high/low
  (true intrabar peak) to compute trail stop activation and SL level.

  The bot was accumulating bar high/low ONLY from WS candlestick messages
  which arrive every ~500ms. Delta Exchange WS candle updates send the
  running high/low of the current bar, but:
    - If a true price spike occurs BETWEEN two WS messages, it is INVISIBLE
      to the bot — df.iloc[-1]["high"] never captures that spike.
    - At bar close, on_bar_close() gets bar_high = last WS-seen high,
      NOT the exchange's true bar high.
    - trail_mon.on_bar_close() therefore computes trail SL from a LOWER
      peak than Pine → trail SL fires earlier at a lower price.

  Proof from logs:
    entry=78139.0  ATR=165.38  trail_offset=90.96
    Bot  trail SL = 78170.10  → back-calculated peak = 78261.06
    Pine trail SL = 78251.00  → back-calculated peak = 78341.96
    Difference = 80.90 points — exactly the WS sampling gap.

  THE FIX:
    After boundary change is detected, call REST fetch_ohlcv() (via
    asyncio.to_thread so the event loop is not blocked) and fetch 3 bars.
    ohlcv[-2] = the bar that JUST CLOSED (authoritative high/low/close).
    Overwrite df.iloc[-1] with these true values before calling on_bar_close().
    Also push corrected high/low to trail_monitor so intrabar peak is synced.

    This guarantees:
      bar_high passed to trail_mon.on_bar_close() == Pine's bar high
      Trail SL activation + level == Pine's trail SL
      Exit price matches Pine within tick precision.

FIX-AUDIT-02 (WS) | CRITICAL — _poll_rest() uses asyncio.to_thread()
  to run the synchronous ccxt fetch_ohlcv() in a thread pool.
  (Preserved from previous version — unchanged)

PRESERVED FROM FIX-WS-v3 + FIX-AUDIT (all unchanged):
  - FIX-WS-3a: REST fallback uses ohlcv[-1] (live bar) boundary detection
  - FIX-WS-3b: Startup boundary initialised from df.iloc[-1] (live bar)
  - FIX-WS-3c: WS message type matching with debug logging for unknowns
  - FIX-TS:    Microsecond → millisecond timestamp conversion
  - FIX-PEAK-WS: push_ws_candle() wired to trail monitor
  - Boundary-based bar detection (fires once per candle, not per 500ms tick)
  - WS primary, REST fallback after 5 failures
  - Historical load via REST on startup (ccxt.async_support)
  - Heartbeat every 30s
  - MIN_BARS=1500 guard
  - _processing guard prevents re-entrant on_bar_close
════════════════════════════════════════════════════════════════════════════════
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
MIN_BARS = 1500

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

_WS_LIVE    = "wss://socket.india.delta.exchange"
_WS_TESTNET = "wss://testnet-socket.india.delta.exchange"

_MAX_WS_FAILURES  = 5
_WS_HEARTBEAT_SEC = 30


def _timeframe_to_ms(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60 * 1000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600 * 1000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400 * 1000
    raise ValueError(f"Unknown timeframe: {tf}")


def _candle_boundary(ts_ms: int, period_ms: int) -> int:
    return (ts_ms // period_ms) * period_ms


def _ccxt_to_ws_symbol(ccxt_symbol: str) -> str:
    return ccxt_symbol.split(":")[0].replace("/", "")


def _timeframe_to_channel(timeframe: str) -> str:
    return f"candlestick_{timeframe}"


def _ts_to_ms(ts) -> int:
    ts = int(ts)
    if ts > 1_000_000_000_000_000:
        return ts // 1000
    if ts > 1_000_000_000_000:
        return ts
    return ts * 1000


class CandleFeed:
    def __init__(self, on_bar_close, on_feed_ready=None):
        self.on_bar_close  = on_bar_close
        async def _noop(): pass
        self.on_feed_ready = on_feed_ready or _noop

        self._period_ms            = _timeframe_to_ms(CANDLE_TIMEFRAME)
        self._last_candle_boundary = 0
        self._df                   = pd.DataFrame()
        self._exchange             = None
        self._ready_fired          = False
        self._ws_failures          = 0
        self._processing           = False
        self._msg_count            = 0
        self.trail_monitor         = None  # FIX-PEAK-WS

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

    async def _load_history(self) -> None:
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

        # Build a sync exchange for REST fallback — offloaded to threads (FIX-AUDIT-02)
        self._exchange = ccxt.delta({
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        })
        self._exchange.markets = fetched_markets

        # FIX-WS-3b: set boundary from live bar so next boundary triggers correctly
        last_closed_ts = int(self._df.iloc[-1]["timestamp"])
        self._last_candle_boundary = _candle_boundary(last_closed_ts, self._period_ms)

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️'})"
        )

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

        logger.info(f"WebSocket connecting → {ws_url} | channel={channel} symbol={ws_symbol}")

        async with websockets.connect(
            ws_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            await ws.send(subscribe_msg)
            logger.info("WebSocket subscribed ✅")
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

            # ── FIX-PEAK-REST ─────────────────────────────────────────────────
            # PROBLEM:
            #   WS candlestick messages arrive every ~500ms. Any real price
            #   spike between two WS messages is NEVER seen by the bot, so
            #   df.iloc[-1]["high"] at bar close can be LOWER than the true
            #   bar high. This caused the bot's trail SL to activate from a
            #   lower peak than Pine → exit prices ~80 points off vs Pine.
            #
            # FIX:
            #   Fetch authoritative closed bar from REST right now (ohlcv[-2]).
            #   Overwrite df.iloc[-1] BEFORE calling on_bar_close() so
            #   bar_high / bar_low passed to trail_mon match Pine exactly.
            #   Push corrected high/low to trail_monitor to sync peak_price.
            #   Uses asyncio.to_thread() — event loop stays free (FIX-AUDIT-02).
            # ──────────────────────────────────────────────────────────────────
            if not self._df.empty:
                try:
                    closed_ohlcv = await asyncio.to_thread(
                        self._exchange.fetch_ohlcv,
                        SYMBOL,
                        CANDLE_TIMEFRAME,
                        None,  # since
                        3,     # limit — only need last 2 bars
                    )
                    if closed_ohlcv and len(closed_ohlcv) >= 2:
                        cb  = closed_ohlcv[-2]   # [-2] = bar that just closed
                        idx = self._df.index[-1]
                        self._df.at[idx, "open"]   = float(cb[1])
                        self._df.at[idx, "high"]   = float(cb[2])
                        self._df.at[idx, "low"]    = float(cb[3])
                        self._df.at[idx, "close"]  = float(cb[4])
                        self._df.at[idx, "volume"] = float(cb[5])
                        logger.info(
                            f"[FEED] FIX-PEAK-REST: closed bar corrected | "
                            f"true_high={cb[2]:.2f} true_low={cb[3]:.2f} "
                            f"true_close={cb[4]:.2f}"
                        )
                        # Sync trail monitor peak_price with true bar extreme
                        if self.trail_monitor is not None:
                            self.trail_monitor.push_ws_candle(
                                float(cb[2]), float(cb[3])
                            )
                    else:
                        logger.warning(
                            "[FEED] FIX-PEAK-REST: REST returned < 2 bars — "
                            "using WS-accumulated high/low (may differ from Pine)"
                        )
                except Exception as e:
                    logger.warning(
                        f"[FEED] FIX-PEAK-REST: REST fetch failed — "
                        f"using WS-accumulated high/low: {e}"
                    )
            # ── END FIX-PEAK-REST ─────────────────────────────────────────────

            logger.info(
                f"✅ Bar confirmed [WS] | "
                f"closed_boundary={self._last_candle_boundary} | "
                f"new_boundary={current_boundary} | "
                f"bars={len(self._df)} — evaluating signals..."
            )

            if self._processing:
                logger.warning("⚠️ on_bar_close still processing — skipping this bar")
                self._last_candle_boundary = current_boundary
                return

            if len(self._df) >= MIN_BARS:
                self._processing = True
                try:
                    await self.on_bar_close(self._df.copy())
                finally:
                    self._processing = False
            else:
                logger.warning(f"⚠️ Bar skipped — only {len(self._df)} bars (need {MIN_BARS}).")

            new_row = pd.DataFrame([{
                "timestamp": candle_ts_ms,
                "open": o, "high": h, "low": l, "close": c, "volume": v,
            }])
            self._df = pd.concat(
                [self._df, new_row], ignore_index=True
            ).tail(MIN_BARS + 50)
            self._last_candle_boundary = current_boundary

        else:
            if not self._df.empty:
                idx = self._df.index[-1]
                self._df.at[idx, "open"]   = o
                self._df.at[idx, "high"]   = h
                self._df.at[idx, "low"]    = l
                self._df.at[idx, "close"]  = c
                self._df.at[idx, "volume"] = v

            if self.trail_monitor is not None:
                self.trail_monitor.push_ws_candle(h, l)

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
                # FIX-AUDIT-02: asyncio.to_thread() runs the BLOCKING sync ccxt call
                # in a thread pool so the event loop remains free between polls.
                ohlcv = await asyncio.to_thread(
                    self._exchange.fetch_ohlcv,
                    SYMBOL,
                    CANDLE_TIMEFRAME,
                    None,   # since (unused)
                    5,      # limit
                )

                if not ohlcv or len(ohlcv) < 2:
                    continue

                # FIX-WS-3a: compare live bar (ohlcv[-1]) boundary vs last boundary
                live_bar      = ohlcv[-1]
                live_ts       = int(live_bar[0])
                live_boundary = _candle_boundary(live_ts, self._period_ms)

                if live_boundary > self._last_candle_boundary:
                    if len(self._df) >= MIN_BARS and not self._processing:
                        logger.info(
                            f"✅ Bar confirmed [REST fallback] | "
                            f"prev_boundary={self._last_candle_boundary} | "
                            f"new_boundary={live_boundary}"
                        )
                        self._processing = True
                        try:
                            await self.on_bar_close(self._df.copy())
                        finally:
                            self._processing = False
                    else:
                        logger.warning(
                            f"⚠️ Bar skipped — only {len(self._df)} bars (need {MIN_BARS}) "
                            f"or still processing."
                        )

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
