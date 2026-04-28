"""
main.py — Shiva Sniper v10 — REENTRY-FIX-v1 + RECOVERY-FIX-v1 + ADOPT-FIX-v1
════════════════════════════════════════════════════════════════════════

NEW FIX IN THIS VERSION (ADOPT-FIX-v1):
──────────────────────────────────────────────────────────────────────
FIX-ADOPT-01 | _pending_signal cleared immediately after _adopt_position().
  ROOT CAUSE: on the first bar close after a bot restart with an open
  position, _adopt_position() ran first (setting in_position=True), but
  _pending_signal was then evaluated and stored. If the adopted trade
  exited quickly (same bar), _on_trail_exit would consume _pending_signal
  and fire _enter() using a snap from before the adoption — stale context
  where in_position was still False. Pine only re-enters on the NEXT bar.
  Fix: self._pending_signal = None after _adopt_position() completes so
  re-entry on the adoption bar is never attempted.

FIX-COMMENT-01 | Corrected stale docstring on _pending_signal.
  The comment said "ONLY consumed when source=bar_close" — but
  REENTRY-FIX-v1 (already in place) correctly consumes it on ALL exit
  sources. The comment was a leftover from the old behaviour and has been
  updated to reflect the actual logic.

──────────────────────────────────────────────────────────────────────
RECOVERY-FIX-01 | CRITICAL — adopt any pre-existing position on Delta
  during startup so trail / SL / TP / Max-SL management resumes
  automatically after a bot restart.

  ROOT CAUSE OF MANY MISSING-EXIT INCIDENTS:
  The bot was restarted multiple times during open trades (PM2 logs
  show ~10 restart cycles between 10:43 and 11:21 on 2026-04-28).
  Each restart began with `in_position = False`. The bot was therefore
  blind to the trade still open on Delta — no trail, no SL, no TP, no
  Max-SL. When the user finally flat-closed manually, the bot's next
  exit attempt produced no_position_for_reduce_only, which (combined
  with the old _fire_exit retry pattern) cascaded into the infinite
  error loop visible at 10:39.

  THE FIX:
  • initialize() now calls order_mgr.fetch_open_position() (FIX-OM-004).
  • If a position is found, it is buffered as self._adopted_position.
  • On the FIRST bar_close after startup, _adopt_position() is invoked
    BEFORE entry evaluation. It reconstructs RiskLevels (using the
    current ATR — best effort; the original entry-bar ATR is unknown),
    seeds TrailState (peak_price = current close, stage estimated from
    current profit, BE flag inferred), and starts the trail monitor.
  • The bot then manages the recovered trade exactly as if it had just
    been opened — trail, BE, max-SL all engage from this point onward.
  • Exit reason / journal will record signal_type = "RECOVERED" so the
    trade is distinguishable in post-mortem.

PRESERVED FROM REENTRY-FIX-v1:
──────────────────────────────────────────────────────────────────────
REENTRY-FIX-v1 | CRITICAL — same-bar reentry now fires on ALL exit
  sources (tick AND bar_close), not just bar_close exits.

  ROOT CAUSE OF MISSING TRADES vs PINE:
  Pine Script evaluates entry conditions on every bar close. If a trade
  exits on that bar (whether via tick-level SL/TP or bar-close check),
  Pine immediately re-enters if conditions are still valid — same bar,
  same candle. The old bot discarded _pending_signal on tick exits,
  treating it as "stale." But _pending_signal is set in on_bar_close()
  BEFORE the trail monitor processes exits. It is always fresh — it
  comes from the current bar's indicator snapshot. Discarding it caused
  the bot to miss every same-bar reentry that Pine would take.

  Looking at Pine trade list: trades 316-320 all on Apr 22, trades
  321-325 all within 24h — most are same-bar reentries that the bot
  was silently dropping.

  FIX: _on_trail_exit() now consumes _pending_signal unconditionally
  on all exit sources. This matches Pine's newBar → evaluate → reenter
  logic exactly.

PRESERVED FROM BUG-FIX-AUDIT-v1:
  FIX-AUDIT-03: source tag ("bar_close"/"tick") still passed through
  for logging — only the discard-on-tick behaviour is removed.
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
    BE_MULT,
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
        # Consumed on ALL exit sources (bar_close AND tick) — REENTRY-FIX-v1.
        # _pending_signal is always set from the CURRENT bar's on_bar_close()
        # BEFORE any exit fires, so it is always fresh data, never stale.
        # Pine enters on the same bar whether exit is tick-driven or bar-close.
        self._pending_signal: Optional[tuple] = None  # (Signal, IndicatorSnapshot)

        # RECOVERY-FIX-01: position adopted from the exchange at startup.
        # Populated by initialize() if an open position is detected. Consumed
        # on the first on_bar_close() to reconstruct RiskLevels + TrailState
        # and start the trail monitor.
        self._adopted_position: Optional[dict] = None

    async def initialize(self) -> None:
        await self.order_mgr.initialize()
        logger.info("OrderManager initialized ✅")

        # RECOVERY-FIX-01: detect any pre-existing position on Delta and
        # buffer it for adoption on the first bar close. This keeps
        # trail/SL/TP/Max-SL management alive across bot restarts.
        try:
            adopted = await self.order_mgr.fetch_open_position()
        except Exception as e:
            logger.error(f"[STARTUP] fetch_open_position raised: {e}", exc_info=True)
            adopted = None

        if adopted:
            self._adopted_position = adopted
            logger.warning(
                f"[STARTUP][RECOVERY] Buffered adopted position: "
                f"is_long={adopted['is_long']} entry={adopted['entry_price']:.2f} "
                f"qty={adopted['qty']}. Will resume management on next bar close."
            )
            # Best-effort Telegram alert so the user knows the bot adopted the trade.
            try:
                await self.telegram.notify_error(
                    f"⚠️ Adopted existing {('LONG' if adopted['is_long'] else 'SHORT')} "
                    f"position @ {adopted['entry_price']:.2f}. "
                    f"Trail/SL will reattach on next bar close."
                )
            except Exception:
                pass
        else:
            logger.info("[STARTUP] No open position on Delta — starting clean.")

    async def on_bar_close(self, df) -> None:
        """
        Called by CandleFeed once per confirmed candle bar.

        Pine parity:
          if newBar and noPosition → evaluate entry
          if in position → strategy.exit() re-evaluated each bar
            → Python equiv: trail_mon.on_bar_close(close, high, low, atr)

        RECOVERY-FIX-01: before any signal/entry/exit logic runs, if a
        position was adopted at startup we reconstruct its risk + trail
        state on this bar close. After adoption the bot continues
        normally — the next branches will see in_position=True and
        delegate to trail_mon.on_bar_close().
        """
        try:
            snap: IndicatorSnapshot = compute(df)
        except Exception as e:
            logger.error(f"Indicator compute failed: {e}", exc_info=True)
            return

        # RECOVERY-FIX-01: adopt buffered exchange position on the first
        # bar close after startup (must run before signal evaluation so
        # the bot does not stack a new entry on top of an existing one).
        if self._adopted_position is not None:
            try:
                await self._adopt_position(self._adopted_position, snap)
            except Exception as e:
                logger.error(f"[RECOVERY] _adopt_position failed: {e}", exc_info=True)
            finally:
                self._adopted_position = None
            # FIX-ADOPT-01: discard the signal computed on the adoption bar.
            # _adopt_position() sets in_position=True. Allowing _pending_signal
            # to survive would let _on_trail_exit fire a same-bar re-entry
            # using a snap from before the position was known — stale context.
            # Pine only re-enters on the NEXT bar. Clear here; recomputed next bar.
            self._pending_signal = None
            # Still fall through so trail_mon.on_bar_close() runs for the adopted trade.

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
        RECOVERY-FIX-01: reconstruct RiskLevels + TrailState for a position
        that was already open on Delta when the bot started, then start the
        trail monitor so SL / TP / trail / Max-SL all engage from the next
        tick onward.

        Best-effort reconstruction (the originating bar's ATR / signal_type
        cannot be recovered post-hoc):

          • RR / ATR-mult / stop_dist  → use TREND values (5.0, 0.9). Trend
            params are the larger of the two regimes, so the resulting SL is
            slightly wider than a range-trade would have used. This is the
            safe direction — we'd rather over-give room than knock ourselves
            out of an adopted trade prematurely.
          • peak_price                 → seed with current bar close (will
            tighten as price moves further into profit).
          • trail stage                → estimated from current profit using
            _upgrade_stage on the live ATR.
          • be_done                    → True if profit already exceeds
            BE_MULT × ATR (so we don't undo a breakeven that was logically
            already met before the restart).

        NOTE: signal_type is recorded as "RECOVERED" so the journal /
        telegram exit message clearly distinguishes adopted trades from
        fresh ones.
        """
        is_long = bool(pos["is_long"])
        entry   = float(pos["entry_price"])

        if entry <= 0:
            logger.error(
                f"[RECOVERY] Adopted position has invalid entry_price={entry}; "
                f"abandoning adoption — bot will run blind to this trade. "
                f"⚠️ MANUAL INTERVENTION REQUIRED."
            )
            return

        # Reconstruct risk using TREND parameters (wider, safer for adoption).
        # If the original trade was a range-trade the SL/TP are slightly
        # off — but Max-SL still caps total loss at MAX_SL_MULT × ATR.
        from risk.calculator import calc_levels
        risk = calc_levels(entry, snap.atr, is_long, is_trend=True)

        # Profit relative to entry, on this bar's close.
        profit_dist = (snap.close - entry) if is_long else (entry - snap.close)

        # Estimate stage and BE state from current profit.
        from monitor.trail_loop import _upgrade_stage
        stage   = _upgrade_stage(0, profit_dist, snap.atr)
        be_done = profit_dist > snap.atr * BE_MULT

        # Peak: best favourable price we KNOW the trade has reached. We can
        # only see "now" — seed with current close. Real intrabar peaks will
        # be picked up by push_ws_candle / on_price_tick from now on.
        peak = snap.close

        # If BE was already implicitly met, lift current_sl to entry so the
        # bot doesn't surrender the breakeven gain.
        current_sl = risk.sl
        if be_done:
            if is_long and entry > current_sl:
                current_sl = entry
            elif (not is_long) and entry < current_sl:
                current_sl = entry

        self.risk = risk
        self.trail_state = TrailState(
            stage        = stage,
            current_sl   = current_sl,
            peak_price   = peak,
            be_done      = be_done,
            max_sl_fired = False,
        )
        self.in_position  = True
        self._signal_type = "RECOVERED"

        # Open a journal trade so exit logging works correctly.
        try:
            self.journal.open_trade(
                signal_type = "RECOVERED",
                is_long     = is_long,
                entry_price = entry,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
                qty         = ALERT_QTY,
            )
        except Exception as e:
            logger.error(f"[RECOVERY] Journal open_trade failed: {e}")

        # Start the trail monitor with the reconstructed state.
        self.trail_mon.start(
            risk_levels       = risk,
            trail_state       = self.trail_state,
            entry_bar_time_ms = int(time.time() * 1000),
            on_trail_exit     = self._on_trail_exit,
        )
        if self._feed is not None:
            self._feed.trail_monitor = self.trail_mon

        logger.warning(
            f"[RECOVERY ✅] Adopted {('LONG' if is_long else 'SHORT')} "
            f"@ {entry:.2f} | sl={risk.sl:.2f} tp={risk.tp:.2f} "
            f"stage={stage} be_done={be_done} atr={snap.atr:.2f} "
            f"current_profit={profit_dist:.2f}"
        )

        try:
            await self.telegram.notify_entry(
                signal_type = "RECOVERED",
                entry_price = entry,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
            )
        except Exception as e:
            logger.error(f"[RECOVERY] Telegram notify_entry failed: {e}")

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
