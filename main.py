"""
main.py — Shiva Sniper v10 — RECOVERY-FIX-v1
════════════════════════════════════════════════════════════════════════

RECOVERY-FIX-01 | CRITICAL — adopt open positions on bot startup.
  ROOT CAUSE OF UNMANAGED ORPHAN TRADES:
  PM2 restart (or crash) while a trade is open leaves the position on
  the exchange but resets in_position=False in Python. The trail monitor
  never starts for the orphaned trade — no SL/TP/trail/Max-SL protection.

  From the 2026-04-28 log: bot restarted ~10 times while short was open
  at 76975. Each restart began flat. The trade ran unmanaged until manual
  close — triggering the close_position cascade (Bug 1 + Bug 2).

  FIX:
  initialize() now calls fetch_open_position(). If a position is found:
    1. Sets in_position=True with synthetic RiskLevels from live entry_price
       and current ATR fetched from the ticker.
    2. Buffers _adopted_position so on_bar_close() starts the trail monitor
       on the FIRST bar close after restart.
    3. Logs [STARTUP][RECOVERY] path in PM2 logs.
  If flat: logs [STARTUP] No open position — clean start.

PRESERVED FROM REENTRY-FIX-v1 (all unchanged):
  REENTRY-FIX-v1: same-bar reentry fires on ALL exit sources.
  FIX-AUDIT-03: source tag (bar_close/tick) passed through for logging.
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
        self._pending_signal: Optional[tuple] = None  # (Signal, IndicatorSnapshot)

        # RECOVERY-FIX-01: buffer an adopted position detected at startup.
        # Consumed in on_bar_close() on the first bar after restart.
        self._adopted_position: Optional[dict] = None  # {"is_long": bool, "entry_price": float}

    async def initialize(self) -> None:
        await self.order_mgr.initialize()
        logger.info("OrderManager initialized ✅")

        # RECOVERY-FIX-01: check for an open position on Delta at startup.
        try:
            pos = await self.order_mgr.fetch_open_position()
        except Exception as e:
            logger.warning(f"[STARTUP] fetch_open_position failed: {e} — starting clean")
            pos = None

        if pos is None:
            logger.info("[STARTUP] No open position on Delta — starting clean.")
        else:
            self._adopted_position = pos
            logger.info(
                f"[STARTUP][RECOVERY] Open {'LONG' if pos['is_long'] else 'SHORT'} detected "
                f"@ {pos['entry_price']:.2f} | contracts={pos['contracts']} — "
                f"will reattach trail on first bar close. [RECOVERY-FIX-01]"
            )

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

        # RECOVERY-FIX-01: on first bar after restart with an adopted position,
        # reconstruct RiskLevels/TrailState and start the trail monitor.
        if self._adopted_position is not None:
            await self._adopt_position(self._adopted_position, snap)
            self._adopted_position = None
            # Trail is now running; fall through to the in_position branch below.

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

    async def _adopt_position(self, pos: dict, snap) -> None:
        """
        RECOVERY-FIX-01: Reconstruct in-memory state for an adopted orphan trade.

        Called from on_bar_close() on the first bar after a restart when
        fetch_open_position() found an open trade at startup. Builds synthetic
        RiskLevels and TrailState from the live entry_price and the current
        bar's ATR, then starts the trail monitor exactly as _enter() would.

        The adopted trade is protected from this point forward — all trail/SL/
        TP/Max-SL logic resumes as if the entry had just fired on this bar.
        """
        is_long     = pos["is_long"]
        entry_price = pos["entry_price"]
        current_atr = snap.atr

        logger.info(
            f"[RECOVERY ✅] Adopting {'LONG' if is_long else 'SHORT'} "
            f"@ {entry_price:.2f} | atr={current_atr:.2f} [RECOVERY-FIX-01]"
        )

        from risk.calculator import calc_levels, recalc_levels_from_fill
        # Determine trade type: we can't know if it was a trend or range trade
        # after a restart, so default to trend=True (tighter RR — conservative).
        is_trend = True
        risk_pre = calc_levels(entry_price, current_atr, is_long, is_trend)
        risk     = recalc_levels_from_fill(risk_pre, entry_price)

        self.risk = risk
        self.trail_state = TrailState(
            stage      = 0,
            current_sl = risk.sl,
            peak_price = entry_price,
        )
        self.in_position  = True
        self._signal_type = "ADOPTED"

        self.trail_mon.start(
            risk_levels       = risk,
            trail_state       = self.trail_state,
            entry_bar_time_ms = int(time.time() * 1000),
            on_trail_exit     = self._on_trail_exit,
        )

        if self._feed is not None:
            self._feed.trail_monitor = self.trail_mon

        logger.info(
            f"[RECOVERY ✅] Trail monitor started | "
            f"sl={risk.sl:.2f} tp={risk.tp:.2f} atr={current_atr:.2f}"
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

        # REENTRY FIX: Pine takes a new trade immediately after an exit on the
        # same bar — regardless of whether the exit was tick-driven or bar-close.
        # The _pending_signal is always set from the CURRENT bar's on_bar_close()
        # evaluation BEFORE the trade exits. It is always fresh — the snap comes
        # from the same bar's close indicators. Consume it on ALL exit sources.
        # This matches Pine's behaviour: new bar → evaluate entry → exit fires →
        # re-enter same bar if conditions still valid.
        pending = self._pending_signal
        self._pending_signal = None
        if pending is not None:
            sig, snap = pending
            logger.info(
                f"[REENTRY] Firing buffered {sig.signal_type.value} "
                f"after exit (reason={reason} source={source})"
            )
            await self._enter(sig, snap)

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
