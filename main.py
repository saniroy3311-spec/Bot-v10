"""
main.py — Shiva Sniper v6.5 Entry Point
════════════════════════════════════════════════════════════════════════

FIXES IN THIS FILE (replaces the broken main.py that had no entry point):
──────────────────────────────────────────────────────────────────────
FIX-MAIN-001 | This file was main.py = orders/manager.py (just a class
  definition, no async main(), no if __name__ == '__main__':).
  Running `python3 main.py` defined the OrderManager class and exited.
  The bot NEVER RAN. This replaces it with the correct entry point.

FIX-MAIN-002 | Correct module wiring:
  CandleFeed (ws_feed.py)
    → on_bar_close(df)
      → indicators/engine.compute(df)        → IndicatorSnapshot
      → strategy/signal.evaluate(snap)       → Signal
      → risk/calculator.calc_levels()        → RiskLevels (pre-fill)
      → orders/manager.place_entry(sl, tp)   → fill price
      → risk/calculator.recalc_levels_from_fill(fill) → RiskLevels (actual)
      → monitor/trail_loop.TrailMonitor.start(risk, state)
      → each bar while in position:
          → trail_loop.on_bar_close(close, high, low, atr)

FIX-MAIN-003 | orders/manager.py (BRACKET mode) is used:
  - Exchange-side SL/TP bracket orders as hard safety net.
  - Python trail loop dynamically tightens on top.
  - If Python crashes, exchange bracket prevents catastrophic loss.

FIX-MAIN-004 | Position state correctly reset on exit via on_trail_exit
  callback. Next bar close will evaluate fresh entry signals.

NOTE: execution.py is a PARALLEL implementation that was never wired
  to a working entry point. It has 5 critical bugs (see AUDIT below).
  Do NOT use execution.py. This file uses the correct modular path:
  orders/ + monitor/ + strategy/ + indicators/ + risk/
════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Optional

from config import (
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

from feed.ws_feed      import CandleFeed
from indicators.engine import compute, IndicatorSnapshot
from strategy.signal   import evaluate, SignalType
from risk.calculator   import (
    RiskLevels, TrailState,
    calc_levels, recalc_levels_from_fill,
)
from orders.manager    import OrderManager
from monitor.trail_loop import TrailMonitor
from infra.telegram    import Telegram
from infra.journal     import Journal

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


class ShivaSniperBot:
    """
    Top-level bot controller.

    State machine:
      IDLE → (signal at bar close) → ENTERING → ACTIVE → (exit) → IDLE
    """

    def __init__(self) -> None:
        self.order_mgr   = OrderManager()
        self.telegram    = Telegram()
        self.journal     = Journal()
        self.trail_mon   = TrailMonitor(self.order_mgr, self.telegram, self.journal)

        self.in_position : bool                 = False
        self.risk        : Optional[RiskLevels] = None
        self.trail_state : Optional[TrailState] = None
        self._entry_lock : asyncio.Lock         = asyncio.Lock()

    # ── Initialisation ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load market map — must complete before feed starts."""
        await self.order_mgr.initialize()
        logger.info("OrderManager initialized ✅")

    # ── Core bar-close handler ────────────────────────────────────────────────

    async def on_bar_close(self, df) -> None:
        """
        Called by CandleFeed once per confirmed 30-minute bar.

        Pine parity:
          if newBar and noPosition → evaluate entry
          if in position → strategy.exit() re-evaluated each bar
            → Python equiv: trail_mon.on_bar_close(close, high, low, atr)
        """
        # ── 1. Compute indicators ─────────────────────────────────────────────
        try:
            snap: IndicatorSnapshot = compute(df)
        except Exception as e:
            logger.error(f"Indicator compute failed: {e}", exc_info=True)
            return

        # ── 2. If in position: notify trail monitor (FIX-TRAIL-14 parity) ────
        #
        # Pine runs strategy.exit() on every bar close, recomputing SL/TP with
        # the current ATR. trail_mon.on_bar_close() mirrors that:
        #   - Recalculates SL + TP with current ATR (FIX-TRAIL-14)
        #   - Upgrades trail stage from bar close profit_dist (FIX-TRAIL-7)
        #   - Updates breakeven state (Pine parity)
        #   - Preserves intra-bar peak extremes (FIX-TRAIL-8)
        if self.in_position:
            if self.trail_mon._running:
                self.trail_mon.on_bar_close(
                    bar_close   = snap.close,
                    bar_high    = snap.high,
                    bar_low     = snap.low,
                    current_atr = snap.atr,
                )
                logger.debug(
                    f"[BAR CLOSE] Position update | "
                    f"close={snap.close:.2f} atr={snap.atr:.2f}"
                )
            else:
                # Trail monitor stopped but in_position still True:
                # exit callback hasn't fired yet or position was manually closed.
                # Reset state to avoid being stuck.
                logger.warning(
                    "[BAR CLOSE] in_position=True but trail_mon not running — "
                    "resetting position state"
                )
                self.in_position = False
                self.risk        = None
                self.trail_state = None
            return   # Pine: no new entry while in position

        # ── 3. No position: evaluate entry signal ─────────────────────────────
        #
        # Pine: if newBar and noPosition { if trendLong ... else if trendShort ... }
        sig = evaluate(snap, has_position=False)

        logger.info(
            f"[BAR] signal={sig.signal_type.value} | "
            f"adx={snap.adx:.2f} trend={snap.trend_regime} "
            f"range={snap.range_regime} filters={snap.filters_ok} | "
            f"close={snap.close:.2f} atr={snap.atr:.2f}"
        )

        if sig.signal_type == SignalType.NONE:
            return

        # ── 4. Entry guard ────────────────────────────────────────────────────
        if self._entry_lock.locked():
            logger.warning("[ENTRY] Lock held — skipping duplicate entry")
            return

        async with self._entry_lock:
            if self.in_position:
                return

            # 4a. Compute risk levels from bar close price (pre-fill estimate)
            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

            logger.info(
                f"[SIGNAL] {sig.signal_type.value} | "
                f"close={snap.close:.2f} "
                f"sl_pre={risk_pre.sl:.2f} tp_pre={risk_pre.tp:.2f} "
                f"atr={snap.atr:.2f}"
            )

            # 4b. Place bracket entry order (SL + TP on exchange)
            try:
                order = await self.order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Order failed: {e}", exc_info=True)
                return

            # 4c. Re-anchor SL/TP to ACTUAL fill price (not bar close estimate)
            fill = float(order.get("average") or order.get("price") or snap.close)
            risk = recalc_levels_from_fill(risk_pre, fill)

            self.risk = risk
            self.trail_state = TrailState(
                stage      = 0,
                current_sl = risk.sl,
                peak_price = fill,
            )
            self.in_position = True

            # 4d. Start tick-resolution trail monitor
            self.trail_mon.start(
                risk_levels       = risk,
                trail_state       = self.trail_state,
                entry_bar_time_ms = int(time.time() * 1000),
                on_trail_exit     = self._on_trail_exit,
            )

            logger.info(
                f"[ENTRY ✅] {sig.signal_type.value} | "
                f"fill={fill:.2f} sl={risk.sl:.2f} tp={risk.tp:.2f} "
                f"atr={snap.atr:.2f} qty={ALERT_QTY} lots"
            )

    # ── Exit callback ─────────────────────────────────────────────────────────

    async def _on_trail_exit(self, exit_price: float, reason: str) -> None:
        """Called by TrailMonitor when any exit condition fires."""
        if not self.in_position:
            return

        entry_px = self.risk.entry_price if self.risk else 0.0
        is_long  = self.risk.is_long     if self.risk else True

        pl_sign = "+" if ((exit_price - entry_px) > 0) == is_long else "-"
        logger.info(
            f"[EXIT ✅] reason={reason} | "
            f"entry={entry_px:.2f} exit={exit_price:.2f} | "
            f"{'LONG' if is_long else 'SHORT'}"
        )

        # Reset state — next bar close evaluates fresh entry
        self.in_position = False
        self.risk        = None
        self.trail_state = None

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await self.initialize()

        feed = CandleFeed(
            on_bar_close  = self.on_bar_close,
            on_feed_ready = self._on_feed_ready,
        )
        logger.info(
            f"Starting Shiva Sniper v6.5 | "
            f"symbol={SYMBOL} tf={CANDLE_TIMEFRAME} qty={ALERT_QTY} lots"
        )
        await feed.start()   # Runs forever

    async def _on_feed_ready(self) -> None:
        logger.info("Feed ready — Shiva Sniper is LIVE 🚀")
        await self.telegram.notify_start()

    async def shutdown(self) -> None:
        """Clean shutdown: close all sessions gracefully."""
        try:
            await self.telegram.notify_stop()
        except Exception:
            pass
        try:
            await self.telegram.close()
        except Exception:
            pass
        try:
            await self.order_mgr.close_exchange()
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main() -> None:
    bot = ShivaSniperBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)
        raise
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(_main())
