"""
main.py — Shiva Sniper v10 — BUG-FIX-AUDIT-v1
════════════════════════════════════════════════════════════════════════

FIXES IN THIS VERSION (on top of FIX-MAIN-001 through FIX-MAIN-004):
──────────────────────────────────────────────────────────────────────
FIX-AUDIT-03 | HIGH — _on_trail_exit() updated to accept `source` param.
  trail_loop._fire_exit() now passes source="bar_close" or source="tick"
  to the exit callback. main.py uses source to decide whether to consume
  _pending_signal:

    source="bar_close" → same-bar exit: signal is fresh (same bar's snap),
      Pine would fire immediately → consume _pending_signal and enter now.

    source="tick" → intrabar exit: _pending_signal snap is from the
      PREVIOUS bar close (could be 29 minutes stale). Pine would NOT enter
      until the NEXT bar close (newBar and noPosition guard). Discard the
      stale signal and let the next on_bar_close() evaluate fresh indicators.

  The old code consumed _pending_signal unconditionally on any exit, causing
  entries with stale ATR / EMA / risk levels on all intrabar tick-loop exits.
════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
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
    State machine: IDLE → ENTERING → ACTIVE → (exit) → IDLE
    """

    def __init__(self) -> None:
        self.order_mgr   = OrderManager()
        self.telegram    = Telegram()
        self.journal     = Journal()
        self.trail_mon   = TrailMonitor(self.order_mgr, self.telegram, self.journal)

        self.in_position  : bool                 = False
        self.risk         : Optional[RiskLevels] = None
        self.trail_state  : Optional[TrailState] = None
        self._entry_lock  : asyncio.Lock         = asyncio.Lock()
        self._feed        : Optional[CandleFeed] = None
        self._signal_type : str                  = ""

        # SAME-BAR-REENTRY: buffer the last signal from bar-close evaluation.
        # ONLY consumed when source="bar_close" exits (FIX-AUDIT-03).
        # For tick-loop exits (source="tick"), this is discarded — the snap
        # would be stale (from the previous bar close) and Pine would not re-
        # enter until the next bar close anyway.
        self._pending_signal: Optional[tuple] = None  # (Signal, IndicatorSnapshot)

    async def initialize(self) -> None:
        await self.order_mgr.initialize()
        logger.info("OrderManager initialized ✅")

    async def on_bar_close(self, df) -> None:
        """
        Called by CandleFeed once per confirmed candle bar.

        Pine parity:
          if newBar and noPosition → evaluate entry
          if in position → strategy.exit() re-evaluated each bar
            → Python equiv: trail_mon.on_bar_close(close, high, low, atr)
        """
        try:
            snap: IndicatorSnapshot = compute(df)
        except Exception as e:
            logger.error(f"Indicator compute failed: {e}", exc_info=True)
            return

        # Evaluate entry signal for same-bar-reentry buffering.
        # IMPORTANT: always pass has_position=False here so the signal is computed
        # as if no position exists. This is intentional — we need to know whether
        # a valid entry signal fires on THIS bar for potential same-bar reentry.
        # For normal (non-same-bar) entries, the in_position guard below prevents
        # acting on this signal while a trade is open.
        sig = evaluate(snap, has_position=False)
        if sig.signal_type != SignalType.NONE:
            self._pending_signal = (sig, snap)
        else:
            self._pending_signal = None

        logger.info(
            f"[BAR] signal={sig.signal_type.value} | "
            f"adx={snap.adx:.2f} trend={snap.trend_regime} "
            f"range={snap.range_regime} filters={snap.filters_ok} | "
            f"close={snap.close:.2f} atr={snap.atr:.2f}"
        )

        if self.in_position:
            if self.trail_mon._running:
                self.trail_mon.on_bar_close(
                    bar_close   = snap.close,
                    bar_high    = snap.high,
                    bar_low     = snap.low,
                    bar_open    = snap.open,
                    current_atr = snap.atr,
                )
                logger.debug(
                    f"[BAR CLOSE] Position update | "
                    f"close={snap.close:.2f} atr={snap.atr:.2f}"
                )
            else:
                logger.warning(
                    "[BAR CLOSE] in_position=True but trail_mon not running — "
                    "resetting position state"
                )
                self.in_position = False
                self.risk        = None
                self.trail_state = None
            return

        if self._pending_signal is None:
            return
        sig, snap = self._pending_signal
        self._pending_signal = None
        await self._enter(sig, snap)

    async def _enter(self, sig, snap) -> None:
        """Place a bracket entry order and start the trail monitor."""
        if self._entry_lock.locked():
            logger.warning("[ENTRY] Lock held — skipping duplicate entry")
            return

        async with self._entry_lock:
            if self.in_position:
                return

            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

            logger.info(
                f"[SIGNAL] {sig.signal_type.value} | "
                f"close={snap.close:.2f} "
                f"sl_pre={risk_pre.sl:.2f} tp_pre={risk_pre.tp:.2f} "
                f"atr={snap.atr:.2f}"
            )

            try:
                order = await self.order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Order failed: {e}", exc_info=True)
                await self.telegram.notify_error(f"⚠️ Entry FAILED ({sig.signal_type.value}): {str(e)[:300]}")
                return

            fill = float(order.get("average") or order.get("price") or snap.close)
            risk = recalc_levels_from_fill(risk_pre, fill)

            self.risk = risk
            self.trail_state = TrailState(
                stage      = 0,
                current_sl = risk.sl,
                peak_price = fill,
            )
            self.in_position  = True
            self._signal_type = sig.signal_type.value

            try:
                await self.telegram.notify_entry(
                    signal_type = sig.signal_type.value,
                    entry_price = fill,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = snap.atr,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Telegram notify failed: {e}")

            try:
                self.journal.open_trade(
                    signal_type = sig.signal_type.value,
                    is_long     = sig.is_long,
                    entry_price = fill,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = snap.atr,
                    qty         = ALERT_QTY,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Journal open_trade failed: {e}")

            self.trail_mon.start(
                risk_levels       = risk,
                trail_state       = self.trail_state,
                entry_bar_time_ms = int(time.time() * 1000),
                on_trail_exit     = self._on_trail_exit,
            )

            if self._feed is not None:
                self._feed.trail_monitor = self.trail_mon

            logger.info(
                f"[ENTRY ✅] {sig.signal_type.value} | "
                f"fill={fill:.2f} sl={risk.sl:.2f} tp={risk.tp:.2f} "
                f"atr={snap.atr:.2f} qty={ALERT_QTY} lots"
            )

    async def _on_trail_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """
        Called by TrailMonitor when any exit condition fires.

        FIX-AUDIT-03: Added `source` parameter.
          source="bar_close" → same-bar exit: _pending_signal is from the same
            bar's snap — fresh data. Pine fires same-bar reentry atomically.
            Consume _pending_signal and enter now.
          source="tick" → intrabar exit: _pending_signal is from the PREVIOUS
            bar close — stale by up to BAR_PERIOD_MS. Pine would NOT re-enter
            until the next bar close (newBar guard). Discard the signal.
        """
        if not self.in_position:
            return

        entry_px    = self.risk.entry_price if self.risk else 0.0
        is_long     = self.risk.is_long     if self.risk else True
        sl          = self.risk.sl          if self.risk else 0.0
        tp          = self.risk.tp          if self.risk else 0.0
        atr         = self.risk.atr         if self.risk else 0.0
        trail_stage = self.trail_state.stage if self.trail_state else 0
        signal_type = self._signal_type or "Unknown"

        logger.info(
            f"[EXIT ✅] reason={reason} source={source} | "
            f"entry={entry_px:.2f} exit={exit_price:.2f} | "
            f"{'LONG' if is_long else 'SHORT'}"
        )

        from risk.calculator import calc_real_pl
        real_pl = calc_real_pl(entry_px, exit_price, is_long, ALERT_QTY)

        try:
            await self.telegram.notify_exit(
                reason      = reason,
                entry_price = entry_px,
                exit_price  = exit_price,
                real_pl     = real_pl,
                is_long     = is_long,
            )
        except Exception as e:
            logger.error(f"[EXIT] Telegram notify failed: {e}")

        try:
            self.journal.log_trade(
                signal_type = signal_type,
                is_long     = is_long,
                entry_price = entry_px,
                exit_price  = exit_price,
                sl          = sl,
                tp          = tp,
                atr         = atr,
                qty         = ALERT_QTY,
                real_pl     = real_pl,
                exit_reason = reason,
                trail_stage = trail_stage,
            )
        except Exception as e:
            logger.error(f"[EXIT] Journal log_trade failed: {e}")

        try:
            self.journal.close_open_trade()
        except Exception as e:
            logger.error(f"[EXIT] Journal close_open_trade failed: {e}")

        self.in_position  = False
        self.risk         = None
        self.trail_state  = None
        self._signal_type = ""
        if self._feed is not None:
            self._feed.trail_monitor = None

        # FIX-AUDIT-03: SAME-BAR-REENTRY — only consume _pending_signal for
        # same-bar exits. For intrabar tick-loop exits, the snap in
        # _pending_signal is stale. Discard it and wait for the next bar close.
        if source == "bar_close":
            pending = self._pending_signal
            self._pending_signal = None
            if pending is not None:
                sig, snap = pending
                logger.info(
                    f"[SAME-BAR-REENTRY] Firing buffered {sig.signal_type.value} "
                    f"signal after same-bar exit (reason={reason})"
                )
                await self._enter(sig, snap)
        else:
            # Intrabar exit — discard stale signal, wait for next bar close.
            if self._pending_signal is not None:
                stale_sig, _ = self._pending_signal
                logger.info(
                    f"[TICK EXIT] Discarding stale _pending_signal "
                    f"({stale_sig.signal_type.value}) — "
                    f"source={source}, reason={reason}. "
                    f"Waiting for next bar close to re-evaluate."
                )
            self._pending_signal = None

    async def run(self) -> None:
        await self.initialize()

        feed = CandleFeed(
            on_bar_close  = self.on_bar_close,
            on_feed_ready = self._on_feed_ready,
        )
        self._feed = feed
        logger.info(
            f"Starting Shiva Sniper v10 | "
            f"symbol={SYMBOL} tf={CANDLE_TIMEFRAME} qty={ALERT_QTY} lots"
        )
        await asyncio.gather(
            feed.start(),
            self._daily_summary_loop(),
        )

    async def _on_feed_ready(self) -> None:
        logger.info("Feed ready — Shiva Sniper is LIVE 🚀")
        await self.telegram.notify_start()

    async def _daily_summary_loop(self) -> None:
        IST = timezone(timedelta(hours=5, minutes=30))
        while True:
            try:
                now      = datetime.now(IST)
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                wait_sec = (tomorrow - now).total_seconds()
                logger.info(
                    f"[DAILY] Next summary in {wait_sec/3600:.1f}h "
                    f"({tomorrow.strftime('%Y-%m-%d 00:00 IST')})"
                )
                await asyncio.sleep(wait_sec)
                date_str = now.strftime("%Y-%m-%d")
                summary  = self.journal.get_daily_summary(date_str)
                logger.info(f"[DAILY] Sending summary for {date_str}: {summary}")
                await self.telegram.notify_daily_summary(summary)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[DAILY] Summary loop error: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def shutdown(self) -> None:
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


async def _main() -> None:
    bot = ShivaSniperBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Bot crashed: {e}", exc_info=True)
        try:
            await bot.telegram.notify_crash(tb)
        except Exception:
            pass
        raise
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(_main())
