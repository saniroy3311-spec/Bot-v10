"""
feed/ws_feed.py  —  Shiva Sniper v10  (DELTA-TICK-FIX-v1)
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
    BINANCE_SIGNAL_FEED, BINANCE_SYMBOL,
    TRAIL_EXIT_FROM_DELTA_WS,
)

logger   = logging.getLogger(__name__)

# SATURATION FIX: Increased from 450 to 1000 bars to guarantee 99.9% saturation of the EMA(200)
# Matches TradingView's deep historical calculation exactly, fixing trade count offsets.
MIN_BARS = 1000

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

_WS_LIVE    = "wss://socket.india.delta.exchange"
_WS_TESTNET = "wss://testnet-socket.india.delta.exchange"

_MAX_WS_FAILURES           = 5    
_WS_RETRY_AFTER_REST_POLLS = 60   
_WS_HEARTBEAT_SEC          = 30

_BINANCE_DELTA_DIVERGENCE_MAX = 15.0

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
        self._rest_poll_count      = 0     
        self._processing           = False
        self._msg_count            = 0
        self.trail_monitor         = None  
        self._last_delta_tick: Optional[float] = None

    @property
    def last_delta_tick(self) -> Optional[float]:
        return self._last_delta_tick

    async def start(self) -> None:
        await self._load_history()
        if not self._ready_fired:
            self._ready_fired = True
            await self.on_feed_ready()

        while True:
            if self._ws_failures < _MAX_WS_FAILURES:
                try:
                    await self._run_websocket()
                    self._ws_failures += 1
                except Exception as e:
                    self._ws_failures += 1
                    logger.error(f"WebSocket feed error (failure {self._ws_failures}/{_MAX_WS_FAILURES}): {e}")

                if self._ws_failures < _MAX_WS_FAILURES:
                    wait = min(WS_RECONNECT_SEC * (2 ** (self._ws_failures - 1)), 60)
                    logger.info(f"Reconnecting in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    logger.warning(
                        f"WebSocket failed {_MAX_WS_FAILURES} times — "
                        f"switching to REST polling. Will retry WS after "
                        f"{_WS_RETRY_AFTER_REST_POLLS} polls. [FIX-BUG4]"
                    )
                    self._rest_poll_count = 0
            else:
                try:
                    await self._poll_rest_once()
                except Exception as e:
                    logger.error(f"REST poll error: {e}", exc_info=True)
                    await asyncio.sleep(WS_RECONNECT_SEC)
                    continue

                self._rest_poll_count += 1
                if self._rest_poll_count >= _WS_RETRY_AFTER_REST_POLLS:
                    logger.info(
                        f"[FIX-BUG4] Attempting WS reconnection after "
                        f"{_WS_RETRY_AFTER_REST_POLLS} REST polls..."
                    )
                    self._ws_failures     = 0
                    self._rest_poll_count = 0

    async def _load_history(self) -> None:
        base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
        delta_params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        }

        exchange = ccxt_async.delta(delta_params)
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
            fetched_markets = dict(exchange.markets)
        finally:
            await exchange.close()

        fetch_limit = MIN_BARS + 50

        if BINANCE_SIGNAL_FEED:
            logger.info(
                f"Loading {fetch_limit} historical bars via Binance REST "
                f"for [{BINANCE_SYMBOL}] [{CANDLE_TIMEFRAME}]..."
            )
            binance_async = ccxt_async.binance({"enableRateLimit": True})
            try:
                await binance_async.load_markets()
                all_ohlcv = []
                earliest_ts = None

                while len(all_ohlcv) < fetch_limit:
                    batch_size = min(fetch_limit - len(all_ohlcv), 1000)
                    if earliest_ts is None:
                        batch = await binance_async.fetch_ohlcv(
                            BINANCE_SYMBOL, CANDLE_TIMEFRAME, limit=batch_size
                        )
                    else:
                        go_back_ms = batch_size * self._period_ms
                        since_ts = earliest_ts - go_back_ms
                        batch = await binance_async.fetch_ohlcv(
                            BINANCE_SYMBOL, CANDLE_TIMEFRAME,
                            since=since_ts, limit=batch_size
                        )
                    if not batch:
                        break

                    if earliest_ts is None:
                        all_ohlcv = batch
                    else:
                        cutoff = earliest_ts
                        older = [b for b in batch if int(b[0]) < cutoff]
                        all_ohlcv = older + all_ohlcv

                    earliest_ts = int(all_ohlcv[0][0])
                    logger.info(
                        f"[BINANCE-SIGNAL] Fetched {len(all_ohlcv)}/{fetch_limit} bars..."
                    )

                    if len(batch) < batch_size:
                        break 

                self._df = self._to_df(all_ohlcv[-fetch_limit:])
                logger.info(
                    f"[BINANCE-SIGNAL] Loaded {len(self._df)} Binance bars ✅ "
                    f"(indicators will match Pine's data source)"
                )
            finally:
                await binance_async.close()

            self._binance_exchange = ccxt.binance({"enableRateLimit": True})
            self._binance_exchange.markets = dict(binance_async.markets)
        else:
            logger.info(
                f"Loading {fetch_limit} historical bars via Delta REST "
                f"for [{SYMBOL}] [{CANDLE_TIMEFRAME}]..."
            )
            delta_async = ccxt_async.delta(delta_params)
            try:
                await delta_async.load_markets()
                ohlcv = await delta_async.fetch_ohlcv(
                    SYMBOL, CANDLE_TIMEFRAME, limit=fetch_limit
                )
                self._df = self._to_df(ohlcv)
            finally:
                await delta_async.close()

            self._binance_exchange = None

        self._exchange = ccxt.delta({
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": base_url, "private": base_url}},
        })
        self._exchange.markets = fetched_markets

        if len(self._df) >= 2:
            last_closed_ts = int(self._df.iloc[-2]["timestamp"])
            self._df = self._df.iloc[:-1].copy()
        else:
            last_closed_ts = int(self._df.iloc[-1]["timestamp"])
        self._last_candle_boundary = _candle_boundary(last_closed_ts, self._period_ms)

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️'}) "
            f"[source={'Binance' if BINANCE_SIGNAL_FEED else 'Delta'}]"
        )

    async def _run_websocket(self) -> None:
        ws_url    = _WS_TESTNET if DELTA_TESTNET else _WS_LIVE
        ws_symbol = _ccxt_to_ws_symbol(SYMBOL)
        channel   = _timeframe_to_channel(CANDLE_TIMEFRAME)

        subscribe_msg = json.dumps({
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": channel,    "symbols": [ws_symbol]},
                    {"name": "v2/ticker", "symbols": [ws_symbol]},
                ]
            }
        })
        heartbeat_msg = json.dumps({"type": "heartbeat"})

        logger.info(
            f"WebSocket connecting → {ws_url} | "
            f"channels={channel},v2/ticker symbol={ws_symbol}"
        )

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
                if self._msg_count <= 10 and msg_type not in (
                    channel, "v2/ticker", "subscriptions", "heartbeat"
                ):
                    logger.debug(f"WS msg #{self._msg_count} type={msg_type!r}")

                if msg_type == "v2/ticker":
                    data = msg.get("data") or msg
                    if data:
                        raw_price = (
                            data.get("mark_price") or
                            data.get("last_price") or
                            data.get("close") or
                            0
                        )
                        try:
                            delta_price = float(raw_price)
                        except (TypeError, ValueError):
                            delta_price = 0.0
                        if delta_price > 0 and self.trail_monitor is not None:
                            self._last_delta_tick = delta_price
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                self.trail_monitor.push_delta_tick(delta_price)
                            )
                    continue

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
            if not self._df.empty:
                try:
                    if BINANCE_SIGNAL_FEED and self._binance_exchange is not None:
                        closed_ohlcv = await asyncio.to_thread(
                            self._binance_exchange.fetch_ohlcv,
                            BINANCE_SYMBOL,
                            CANDLE_TIMEFRAME,
                            None,  
                            3,     
                        )
                        bar_idx = -2 if len(closed_ohlcv) >= 2 else -1
                        feed_name = "Binance"
                    else:
                        # FIX-DELTA-CACHE: Delta REST caches bar data briefly after close.
                        # Wait 2s to ensure the exchange has finalised the closed bar's volume.
                        await asyncio.sleep(2)
                        closed_ohlcv = await asyncio.to_thread(
                            self._exchange.fetch_ohlcv,
                            SYMBOL,
                            CANDLE_TIMEFRAME,
                            None,
                            5,
                        )
                        # FIX-VOL-BAR-IDX: -2 = just-closed bar (correct).
                        bar_idx = -2 if len(closed_ohlcv) >= 2 else -1
                        feed_name = "Delta"

                    if closed_ohlcv and len(closed_ohlcv) >= 2:
                        cb  = closed_ohlcv[bar_idx]
                        idx = self._df.index[-1]
                        self._df.at[idx, "open"]   = float(cb[1])
                        self._df.at[idx, "high"]   = float(cb[2])
                        self._df.at[idx, "low"]    = float(cb[3])
                        self._df.at[idx, "close"]  = float(cb[4])
                        self._df.at[idx, "volume"] = float(cb[5])
                        logger.info(
                            f"[FEED] FIX-PEAK-REST [{feed_name}]: closed bar corrected | "
                            f"true_high={cb[2]:.2f} true_low={cb[3]:.2f} "
                            f"true_close={cb[4]:.2f}"
                        )
                        logger.info(
                            f"[ATR-DIAG] feed={feed_name} bar_open={cb[1]:.2f} "
                            f"bar_high={cb[2]:.2f} bar_low={cb[3]:.2f} bar_close={cb[4]:.2f}"
                        )
                        if self.trail_monitor is not None:
                            self.trail_monitor.push_ws_candle(
                                float(cb[2]), float(cb[3]),
                                source = "binance" if feed_name == "Binance" else "delta",
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

                # FIX-DELTA-VOL: Pine uses Delta India volume for vol filter.
                # When BINANCE_SIGNAL_FEED=true: Binance OHLCV written above has Binance vol
                #   → must re-fetch Delta vol and overwrite.
                # When BINANCE_SIGNAL_FEED=false: Delta OHLCV already written above with bar_idx=-2
                #   → volume is already correct; just log it for observability.
                if BINANCE_SIGNAL_FEED:
                    try:
                        _dvol = await asyncio.to_thread(
                            self._exchange.fetch_ohlcv,
                            SYMBOL, CANDLE_TIMEFRAME, None, 3,
                        )
                        if _dvol and len(_dvol) >= 2:
                            _delta_vol = float(_dvol[-2][5])
                            _idx = self._df.index[-1]
                            self._df.at[_idx, "volume"] = _delta_vol
                            logger.info(
                                f"[FEED] FIX-DELTA-VOL: volume={_delta_vol:.0f} "
                                f"(Delta India — Pine vol filter parity ✅)"
                            )
                    except Exception as _e:
                        logger.warning(f"[FEED] FIX-DELTA-VOL: Delta vol fetch failed — {_e}")
                else:
                    # Delta path — volume already set correctly via bar_idx=-2 above.
                    _idx = self._df.index[-1]
                    _delta_vol = float(self._df.at[_idx, "volume"])
                    logger.info(
                        f"[FEED] FIX-DELTA-VOL: volume={_delta_vol:.0f} "
                        f"(Delta India — Pine vol filter parity ✅)"
                    )

            logger.info(
                f"✅ Bar confirmed [WS] | "
                f"closed_boundary={self._last_candle_boundary} | "
                f"new_boundary={current_boundary} | "
                f"bars={len(self._df)} — evaluating signals..."
            )

            self._last_candle_boundary = current_boundary

            if self._processing:
                logger.warning("⚠️ on_bar_close still processing — skipping this bar")
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
            if not BINANCE_SIGNAL_FEED and not self._df.empty:
                idx = self._df.index[-1]
                self._df.at[idx, "open"]   = o
                self._df.at[idx, "high"]   = h
                self._df.at[idx, "low"]    = l
                self._df.at[idx, "close"]  = c
                self._df.at[idx, "volume"] = v

            if self.trail_monitor is not None and TRAIL_EXIT_FROM_DELTA_WS:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.trail_monitor.on_price_tick(c, source="delta")
                )
                self.trail_monitor.push_ws_candle(h, l, source="delta")

            if self.trail_monitor is not None:
                self._last_delta_tick = c   
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.trail_monitor.push_delta_tick(c)
                )

    async def _poll_rest_once(self) -> None:
        sleep_sec = 5
        await asyncio.sleep(sleep_sec)

        if BINANCE_SIGNAL_FEED and self._binance_exchange is not None:
            ohlcv = await asyncio.to_thread(
                self._binance_exchange.fetch_ohlcv,
                BINANCE_SYMBOL,
                CANDLE_TIMEFRAME,
                None, 5,
            )
        else:
            ohlcv = await asyncio.to_thread(
                self._exchange.fetch_ohlcv,
                SYMBOL,
                CANDLE_TIMEFRAME,
                None, 5,
            )

        if not ohlcv or len(ohlcv) < 2:
            return

        live_bar      = ohlcv[-1]
        live_ts       = int(live_bar[0])
        live_boundary = _candle_boundary(live_ts, self._period_ms)

        if live_boundary > self._last_candle_boundary:
            if not self._df.empty and len(ohlcv) >= 2:
                try:
                    cb  = ohlcv[-2]   
                    idx = self._df.index[-1]
                    self._df.at[idx, "open"]   = float(cb[1])
                    self._df.at[idx, "high"]   = float(cb[2])
                    self._df.at[idx, "low"]    = float(cb[3])
                    self._df.at[idx, "close"]  = float(cb[4])
                    self._df.at[idx, "volume"] = float(cb[5])
                    logger.info(
                        f"[FEED] FIX-PEAK-REST (REST path): closed bar corrected | "
                        f"true_high={cb[2]:.2f} true_low={cb[3]:.2f} true_close={cb[4]:.2f}"
                    )
                    if self.trail_monitor is not None:
                        self.trail_monitor.push_ws_candle(
                            float(cb[2]), float(cb[3]),
                            source = "binance" if (
                                BINANCE_SIGNAL_FEED and self._binance_exchange is not None
                            ) else "delta",
                        )
                except Exception as e:
                    logger.warning(f"[FEED] FIX-PEAK-REST (REST path) failed: {e}")

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
