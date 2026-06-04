"""
feed/binance_price_feed.py  —  Shiva Sniper v10  (BINANCE-EXIT-FEED-v1)
════════════════════════════════════════════════════════════════════════════════

PURPOSE:
  Provides a real-time Binance aggTrade price stream used EXCLUSIVELY for
  exit monitoring (SL / TP / trail SL).  All indicator calculation and entry
  signals continue to use the existing 30m Binance OHLCV feed unchanged.

WHY BINANCE AND NOT DELTA:
  Pine Script's broker emulator uses TradingView's Binance BTCUSDT data as
  its BTC price reference.  Delta Exchange India live prices run ~100–150 pts
  lower than TradingView's representation.  Using Delta WS prices for exit
  monitoring caused:
    • Initial SL to fire on Delta-only wicks that Pine never saw
    • Trail SL to arm from Delta peaks that Binance never recorded
    • Exits at wrong price levels vs Pine's "Exit TL" labels

  Binance aggTrade stream fixes this:
    • Same price source as Pine's broker emulator          ✅
    • ~10–100 ms latency (faster than Delta WS 500 ms)    ✅
    • No Delta price gap causing phantom SL/TP triggers    ✅
    • Exit trigger price matches Pine's SL/TP level        ✅
    • Fill slippage (vs Pine's exact fill) is unavoidable
      but is now only real market slippage, not a data bug ✅

ARCHITECTURE:
  BinancePriceFeed runs as a separate asyncio task alongside the existing
  CandleFeed.  It does NOT touch the OHLCV dataframe, indicators, or any
  entry logic.  It only calls trail_monitor.on_price_tick(price) and
  trail_monitor.push_ws_candle(high, low) every time a Binance trade arrives.

  CandleFeed's intrabar calls to on_price_tick / push_ws_candle (the two
  lines that were passing Delta WS prices to the trail monitor) must be
  REMOVED from ws_feed.py (see instructions below).

STREAM USED:
  wss://stream.binance.com:9443/ws/btcusdt@aggTrade
  • Fires on every aggregated trade (~10–100 ms intervals on BTC)
  • Payload field 'p' = trade price (string)
  • Much faster than the old Delta WS candle stream (500 ms updates)

  Fallback: wss://stream.binance.com:9443/ws/btcusdt@kline_1m
  • Fires every ~1 s with the running 1m candle high/low/close
  • Use if aggTrade bandwidth is a concern

CHANGES REQUIRED IN ws_feed.py (one section only):
──────────────────────────────────────────────────────
  Find the intrabar block (~line 654):

    if self.trail_monitor is not None:
        loop = asyncio.get_running_loop()
        loop.create_task(self.trail_monitor.on_price_tick(c))   # ← DELETE
        self.trail_monitor.push_ws_candle(h, l)                  # ← DELETE

  Delete BOTH lines (keep the outer `if self.trail_monitor is not None:` block
  only if other code uses it, otherwise delete the whole block).
  The FIX-PEAK-REST call to push_ws_candle(rest_high, rest_low) at bar close
  must be KEPT — it uses Binance-corrected values and is correct.

CHANGES REQUIRED IN main.py:
──────────────────────────────────────────────────────
  After creating the CandleFeed, start BinancePriceFeed:

    self._binance_feed = BinancePriceFeed(self._trail_mon)
    asyncio.get_running_loop().create_task(self._binance_feed.start())

  On shutdown, stop it:
    self._binance_feed.stop()

════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, TYPE_CHECKING

import websockets
import websockets.exceptions

if TYPE_CHECKING:
    from monitor.trail_loop import TrailMonitor

logger = logging.getLogger("feed.binance_price_feed")

# ── Binance WebSocket endpoints ───────────────────────────────────────────────
_BINANCE_AGG_TRADE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
_BINANCE_KLINE_1M_WS  = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"

# How many seconds to wait between reconnection attempts
_RECONNECT_SEC = 3

# Maximum consecutive failures before backing off
_MAX_FAILURES = 10


class BinancePriceFeed:
    """
    Lightweight Binance aggTrade WebSocket feed for exit price monitoring only.

    Pushes each Binance trade price to TrailMonitor.on_price_tick() so that
    SL / TP / trail SL are evaluated against the same price source Pine Script
    uses, eliminating the Delta-vs-Binance price gap that caused phantom exits.

    Does NOT modify the OHLCV dataframe or affect entry signals in any way.
    """

    def __init__(self, trail_monitor: "TrailMonitor") -> None:
        self._trail_mon   : TrailMonitor    = trail_monitor
        self._running     : bool            = False
        self._task        : Optional[asyncio.Task] = None
        self._failures    : int             = 0

        # Intrabar 1m candle tracking for push_ws_candle (high/low updates)
        # We maintain a simple 1m candle accumulator from aggTrade prices so
        # the trail monitor's peak_price updates smoothly between 30m bar closes.
        self._candle_high : float = 0.0
        self._candle_low  : float = float("inf")
        self._candle_open_ts: int = 0   # epoch ms of current 1m candle open

    # ── Public API ────────────────────────────────────────────────────────────

    def start_task(self) -> asyncio.Task:
        """Create and return the asyncio Task. Call inside an async context."""
        self._running = True
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="binance-price-feed"
        )
        logger.info("[BINANCE-PX] Feed started → aggTrade stream")
        return self._task

    def stop(self) -> None:
        """Signal the feed to stop and cancel its task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[BINANCE-PX] Feed stopped.")

    # ── Internal run loop ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Outer reconnect loop — keeps the stream alive indefinitely."""
        while self._running:
            try:
                await self._connect_and_stream()
                if self._running:
                    logger.warning('[BINANCE-PX] Stream ended cleanly — reconnecting in 3s')
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._failures += 1
                wait = min(_RECONNECT_SEC * self._failures, 60)
                logger.warning(
                    f"[BINANCE-PX] Stream error (failure {self._failures}): {e} "
                    f"— reconnecting in {wait}s"
                )
                await asyncio.sleep(wait)

    async def _connect_and_stream(self) -> None:
        """Connect to Binance aggTrade WS and push prices to trail monitor."""
        logger.info(f"[BINANCE-PX] Connecting → {_BINANCE_AGG_TRADE_WS}")
        async with websockets.connect(
            _BINANCE_AGG_TRADE_WS,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            logger.info("[BINANCE-PX] Connected ✅  (aggTrade stream live)")
            self._failures = 0  # reset on clean connect

            async for raw in ws:
                if not self._running:
                    break

                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

                # aggTrade payload: {'e':'aggTrade', 'p':'81234.56', ...}
                if msg.get("e") != "aggTrade":
                    continue

                price_str = msg.get("p")
                if not price_str:
                    continue

                try:
                    price = float(price_str)
                except ValueError:
                    continue

                if price <= 0:
                    continue

                await self._on_trade(price, int(msg.get("T", 0)))

    # ── Trade handler ─────────────────────────────────────────────────────────

    async def _on_trade(self, price: float, trade_ts_ms: int) -> None:
        """
        Called for every Binance aggTrade.

        1. Pushes price to trail_monitor.on_price_tick() — immediate SL/TP check.
        2. Maintains a 1-minute candle accumulator and calls push_ws_candle()
           at the end of each 1m candle, giving the trail monitor updated
           high/low for trail SL computation between 30m bar closes.
        """
        monitor = self._trail_mon
        if monitor is None:
            return

        # ── 1. Immediate SL/TP/trail check ────────────────────────────────────
        # trail_monitor.on_price_tick is idempotent after _exit_fired = True,
        # so spamming it on every trade is safe.
        # FIX-DUAL-SOURCE-B: pass source="binance" explicitly. The trail
        # monitor will capture the Binance→Delta offset on the first tick
        # after entry and translate every subsequent price to Delta-equivalent
        # space before any peak / SL / trail decision.
        if monitor._running and not monitor._exit_fired:
            await monitor.on_price_tick(price, source="binance")

        # ── 2. Update 1m candle accumulator ───────────────────────────────────
        # We bucket trades into 1-minute windows and call push_ws_candle(h, l)
        # once per minute.  This keeps the trail monitor's peak_price in sync
        # with Binance's running high/low — the same data Pine uses for trail
        # arm computation — without flooding push_ws_candle on every trade.
        candle_minute = (trade_ts_ms // 60_000) * 60_000   # floor to 1m

        if candle_minute != self._candle_open_ts:
            # New 1-minute bucket — flush the old one if it had data
            if (
                self._candle_open_ts > 0
                and self._candle_high > 0
                and self._candle_low < float("inf")
                and monitor._running
                and not monitor._exit_fired
            ):
                # FIX-DUAL-SOURCE-B: Binance source — translation applied inside.
                monitor.push_ws_candle(
                    self._candle_high, self._candle_low, source="binance"
                )

            # Reset for the new bucket
            self._candle_open_ts = candle_minute
            self._candle_high    = price
            self._candle_low     = price
        else:
            # Still in the same 1-minute bucket — track high/low
            if price > self._candle_high:
                self._candle_high = price
            if price < self._candle_low:
                self._candle_low = price

            # Also push an intrabar update every trade so the trail monitor
            # sees the latest Binance peak in real time (not just at 1m close).
            # push_ws_candle only updates peak if it's a new extreme — cheap.
            # FIX-DUAL-SOURCE-B: Binance source — translation applied inside.
            if monitor._running and not monitor._exit_fired:
                monitor.push_ws_candle(
                    self._candle_high, self._candle_low, source="binance"
                )
