"""
main.py — Shiva Sniper Bot v10  (Live Runner)
══════════════════════════════════════════════════════════════════════════════

Entry point launched by systemd / PM2 / Docker CMD.

WHAT THIS FILE DOES
───────────────────
  1. Starts CandleFeed (WS primary, REST fallback).
  2. On every confirmed bar close → compute indicators → evaluate Pine
     entry conditions → enter or update trail.
  3. TrailMonitor handles all exits (TP, Trail SL, BE, Max SL) at tick
     resolution via the WS price push path.
  4. Sends Telegram notifications for entry and exit events.
  5. Persists trade records to SQLite (Journal).
  6. On restart mid-trade: detects existing position via fetch_open_position()
     and resumes trail management from the next bar close.

PINE PARITY
───────────
  Entry  : calc_on_every_tick=false → entry fires ONLY at confirmed bar close.
  Exit   : TrailMonitor evaluates TP/SL on every WS tick (on_price_tick) and
           at bar close (on_bar_close). Stage upgrades + BE only at bar close.
  Volume : FILTER_VOL_ENABLED=false by default — Delta REST volumes (~3% of
           TradingView's) are incomparable data sources. ATR + body filters
           still guard against dead/choppy bars.

RUNNING
───────
  python main.py
  systemctl start shiva_sniper
  docker run shiva_sniper_bot
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Optional

# ── Canonical module imports ───────────────────────────────────────────────────
from config import (
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME, FILTER_VOL_ENABLED,
)
from feed.ws_feed       import CandleFeed
from indicators.engine  import compute
from strategy.signal    import evaluate, SignalType
from risk.calculator    import (
    RiskLevels, TrailState,
    calc_levels, recalc_levels_from_fill, calc_real_pl,
)
from monitor.trail_loop import TrailMonitor
from orders.manager     import OrderManager
from infra.telegram     import Telegram
from infra.journal      import Journal

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


# ══════════════════════════════════════════════════════════════════════════════
# ShivaSniperBot
# ══════════════════════════════════════════════════════════════════════════════

class ShivaSniperBot:
    """
    Live bot orchestrator.

    Lifecycle:
      initialize()        — connect exchange, restore any open position
      _feed_ready()       — called once by CandleFeed after history is loaded
      _on_bar_close(df)   — called on every confirmed bar close (WS or REST)
      _on_trail_exit(...) — called by TrailMonitor when position is closed
      shutdown()          — graceful stop (Telegram, journal, exchange)
    """

    def __init__(self) -> None:
        self._order_mgr = OrderManager()
        self._telegram  = Telegram()
        self._journal   = Journal()
        self._trail_mon = TrailMonitor(
            order_mgr = self._order_mgr,
            telegram  = self._telegram,
            journal   = self._journal,
        )
        self._feed: Optional[CandleFeed] = None

        # Position state — reset on each exit
        self._in_position : bool                  = False
        self._risk        : Optional[RiskLevels]  = None
        self._trail_state : Optional[TrailState]  = None
        self._signal_type : str                   = "None"

        # Guards
        self._entry_lock  = asyncio.Lock()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Connect to exchange, log config, restore open position if any."""
        logger.info("═" * 70)
        logger.info("  Shiva Sniper Bot v10 — Starting")
        logger.info(f"  Symbol={SYMBOL}  TF={CANDLE_TIMEFRAME}  Qty={ALERT_QTY} lots")
        logger.info(f"  FILTER_VOL_ENABLED={FILTER_VOL_ENABLED}  (false = full Pine parity)")
        logger.info("═" * 70)

        await self._order_mgr.initialize()

        # ── Startup recovery: adopt any pre-existing open position ─────────────
        existing = await self._order_mgr.fetch_open_position()
        if existing:
            logger.warning(
                f"[STARTUP] Open position detected — will resume trail on next "
                f"bar close. is_long={existing['is_long']} "
                f"entry={existing['entry_price']:.2f}"
            )
            # Build placeholder RiskLevels — SL/TP reconstructed on first bar close
            self._in_position = True
            self._risk = RiskLevels(
                entry_price = existing["entry_price"],
                sl          = 0.0,
                tp          = 0.0,
                stop_dist   = 0.0,
                atr         = 0.0,
                is_long     = existing["is_long"],
                is_trend    = True,
            )
            self._signal_type = "RECOVERED"
            await self._telegram.send(
                f"⚠️ <b>Position Recovery</b>\n"
                f"Bot restarted mid-trade.\n"
                f"Direction: {'LONG' if existing['is_long'] else 'SHORT'}\n"
                f"Entry (approx): {existing['entry_price']:.2f}\n"
                f"Trail management resumes on next bar close."
            )

        await self._telegram.send(
            f"🟢 <b>Shiva Sniper Bot v10 Started</b>\n"
            f"Symbol: <code>{SYMBOL}</code>  TF: <code>{CANDLE_TIMEFRAME}</code>\n"
            f"Qty: <code>{ALERT_QTY} lots</code>\n"
            f"Volume filter: <code>{'ON' if FILTER_VOL_ENABLED else 'OFF (Pine parity)'}</code>"
        )

    async def shutdown(self) -> None:
        """Graceful stop — stop trail, close exchange connection, notify."""
        logger.info("Shutting down...")
        self._trail_mon.stop()
        try:
            await self._telegram.send("🔴 <b>Shiva Sniper Bot Stopped</b>")
        except Exception:
            pass
        try:
            self._journal.close()
        except Exception:
            pass
        try:
            await self._order_mgr.close_exchange()
        except Exception:
            pass
        logger.info("Shutdown complete.")

    # ── Feed callbacks ────────────────────────────────────────────────────────

    async def _feed_ready(self) -> None:
        """Called by CandleFeed once historical bars are loaded."""
        logger.info("Feed ready — waiting for first bar close...")

    async def _on_bar_close(self, df) -> None:
        """
        Called by CandleFeed on every confirmed bar close.

        Pine parity: calc_on_every_tick=false means:
          - Entry signals only fire at confirmed bar close.
          - Stage upgrades + BE only fire at bar close.
          - TP / SL / Trail SL checked at bar close AND intrabar (WS ticks).
        """
        # ── 1. Compute indicators ─────────────────────────────────────────────
        try:
            snap = compute(df)
        except ValueError as e:
            logger.warning(f"[BAR] Not enough bars: {e}")
            return

        logger.info(
            f"[BAR] close={snap.close:.2f}  atr={snap.atr:.2f}  "
            f"adx={snap.adx:.1f}  rsi={snap.rsi:.1f}  "
            f"trend={snap.trend_regime}  range={snap.range_regime}  "
            f"filters={'OK' if snap.filters_ok else 'FAIL'}"
            f"  [atr={snap.atr_ok} body={snap.body_ok} vol={snap.vol_ok}]"
        )

        # ── 2. Trail update for open position ─────────────────────────────────
        if self._in_position:
            if self._trail_mon._running:
                # Normal: trail is active — update with bar data
                self._trail_mon.on_bar_close(
                    bar_close   = snap.close,
                    bar_high    = snap.high,
                    bar_low     = snap.low,
                    bar_open    = snap.open,
                    current_atr = snap.atr,
                )
            else:
                # Recovery: bot was restarted mid-trade — reconstruct and start trail
                if self._risk is not None and self._risk.stop_dist == 0.0:
                    logger.warning(
                        f"[RECOVERY] Rebuilding RiskLevels from live ATR. "
                        f"entry={self._risk.entry_price:.2f}  live_atr={snap.atr:.2f}"
                    )
                    rebuilt = calc_levels(
                        entry_price = self._risk.entry_price,
                        atr         = snap.atr,
                        is_long     = self._risk.is_long,
                        is_trend    = self._risk.is_trend,
                    )
                    rebuilt = recalc_levels_from_fill(rebuilt, self._risk.entry_price)
                    self._risk        = rebuilt
                    self._trail_state = TrailState(
                        stage      = 0,
                        current_sl = rebuilt.sl,
                        peak_price = self._risk.entry_price,
                    )
                    self._trail_mon.start(
                        risk_levels       = rebuilt,
                        trail_state       = self._trail_state,
                        entry_bar_time_ms = int(time.time() * 1000),
                        on_trail_exit     = self._on_trail_exit,
                    )
                    await self._telegram.send(
                        f"♻️ <b>Trail Resumed (Recovery)</b>\n"
                        f"Entry: {rebuilt.entry_price:.2f}\n"
                        f"SL: {rebuilt.sl:.2f}  TP: {rebuilt.tp:.2f}\n"
                        f"ATR (live): {snap.atr:.2f}"
                    )
            # In position — do NOT evaluate new entry signals
            return

        # ── 3. Evaluate entry signals (only when flat) ────────────────────────
        sig = evaluate(snap, has_position=False)

        if sig.signal_type == SignalType.NONE:
            logger.debug("[BAR] No signal.")
            return

        logger.info(
            f"[SIGNAL] {sig.signal_type.value}  "
            f"is_long={sig.is_long}  regime={sig.regime}"
        )

        # ── 4. Place entry ─────────────────────────────────────────────────────
        if self._entry_lock.locked():
            logger.warning("[ENTRY] Lock held — skipping duplicate attempt")
            return

        async with self._entry_lock:
            if self._in_position:
                return  # race-condition guard

            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

            try:
                order = await self._order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Order failed: {e}")
                await self._telegram.send(
                    f"❌ <b>Entry Order FAILED</b>\n"
                    f"Signal: {sig.signal_type.value}\n"
                    f"Error: <code>{e}</code>"
                )
                return

            fill  = float(order.get("average") or order.get("price") or snap.close)
            risk  = recalc_levels_from_fill(risk_pre, fill)

            self._in_position  = True
            self._risk         = risk
            self._signal_type  = sig.signal_type.value
            self._trail_state  = TrailState(
                stage      = 0,
                current_sl = risk.sl,
                peak_price = fill,
            )

            self._trail_mon.start(
                risk_levels       = risk,
                trail_state       = self._trail_state,
                entry_bar_time_ms = int(time.time() * 1000),
                on_trail_exit     = self._on_trail_exit,
            )

            logger.info(
                f"[ENTRY] Filled | type={sig.signal_type.value}  "
                f"fill={fill:.2f}  sl={risk.sl:.2f}  tp={risk.tp:.2f}  "
                f"atr={snap.atr:.2f}  stop_dist={risk.stop_dist:.2f}"
            )

            # Journal
            try:
                self._journal.open_trade(
                    signal_type = sig.signal_type.value,
                    is_long     = sig.is_long,
                    entry_price = fill,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = snap.atr,
                    qty         = ALERT_QTY,
                )
            except Exception as e:
                logger.warning(f"[JOURNAL] open_trade failed: {e}")

            # Telegram entry notification
            await self._telegram.notify_entry(
                signal_type = sig.signal_type.value,
                entry_price = fill,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
                qty         = ALERT_QTY,
            )

    # ── Exit callback ─────────────────────────────────────────────────────────

    async def _on_trail_exit(
        self,
        exit_price: float,
        reason    : str,
        source    : str = "tick",
    ) -> None:
        """Called by TrailMonitor after position is closed on the exchange."""
        if not self._in_position:
            return

        risk = self._risk
        pl   = (
            calc_real_pl(risk.entry_price, exit_price, risk.is_long, ALERT_QTY)
            if risk else 0.0
        )

        logger.info(
            f"[EXIT] reason={reason}  source={source}  "
            f"entry={risk.entry_price if risk else '?'}  "
            f"exit={exit_price:.2f}  pl={pl:+.4f} USDT"
        )

        # Journal
        try:
            if risk:
                self._journal.log_trade(
                    signal_type = self._signal_type,
                    is_long     = risk.is_long,
                    entry_price = risk.entry_price,
                    exit_price  = exit_price,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = risk.atr,
                    qty         = ALERT_QTY,
                    real_pl     = pl,
                    exit_reason = reason,
                    trail_stage = self._trail_state.stage if self._trail_state else 0,
                )
                self._journal.close_open_trade()
        except Exception as e:
            logger.warning(f"[JOURNAL] log_trade failed: {e}")

        # Telegram exit notification
        try:
            await self._telegram.notify_exit(
                reason      = reason,
                entry_price = risk.entry_price if risk else 0.0,
                exit_price  = exit_price,
                real_pl     = pl,
                is_long     = risk.is_long if risk else True,
                qty         = ALERT_QTY,
            )
        except Exception as e:
            logger.warning(f"[TELEGRAM] notify_exit failed: {e}")

        # Reset — bot is flat and ready for next signal
        self._in_position  = False
        self._risk         = None
        self._trail_state  = None
        self._signal_type  = "None"

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Build feed, wire trail monitor, start feed (blocks until shutdown)."""
        await self.initialize()

        feed = CandleFeed(
            on_bar_close  = self._on_bar_close,
            on_feed_ready = self._feed_ready,
        )
        # CRITICAL: wire trail_monitor so WS candle updates push price ticks
        # directly to TrailMonitor.on_price_tick() — this is the primary
        # intrabar exit detection path (FIX-PARITY-02 in trail_loop.py).
        feed.trail_monitor = self._trail_mon
        self._feed = feed

        try:
            await feed.start()
        except asyncio.CancelledError:
            logger.info("Feed cancelled — shutting down.")
        except Exception as e:
            logger.error(f"Feed crashed: {e}", exc_info=True)
            try:
                await self._telegram.send(f"💥 <b>Feed Crashed</b>\n<code>{e}</code>")
            except Exception:
                pass
            raise
        finally:
            await self.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def _main() -> None:
    bot  = ShivaSniperBot()
    loop = asyncio.get_running_loop()

    def _handle_signal(sig_num: int) -> None:
        logger.info(f"Signal {sig_num} — graceful shutdown initiated...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda sn=s: _handle_signal(sn))
        except NotImplementedError:
            pass  # Windows

    await bot.run()


if __name__ == "__main__":
    asyncio.run(_main())


# ── Backward-compat re-exports so old phase scripts keep working ───────────────
from orders.manager     import OrderManager, build_exchange          # noqa: E402,F401
from monitor.trail_loop import TrailMonitor                          # noqa: E402,F401
from indicators.engine  import IndicatorSnapshot, Signal, SignalType # noqa: E402,F401
from risk.calculator    import RiskLevels, TrailState                # noqa: E402,F401
from execution import ExecutionEngine, log_signal                    # noqa: E402,F401
