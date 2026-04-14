"""
monitor/trail_loop.py — Shiva Sniper v6.5 (PINE-EXACT-v4)

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
FIX-TRAIL-1 | Peak reset to bar_close at every bar boundary (Bug 1 fix)
  Root cause:
    Tick loop tracks peak_price intrabar all through bar N.
    When bar N closes, peak_price is already at bar N's highest tick.
    on_bar_close() upgrades stage using close_profit_dist — correct.
    But tick loop immediately fires exit because the elevated peak_price
    already satisfies the new stage's trail offset.
    Result: exit fires at start of bar N+1 using bar N's peak — wrong.

  Fix:
    At the end of on_bar_close(), reset peak_price = bar_close.
    Tick loop starts bar N+1 tracking peak fresh from bar_close.
    Trail SL at bar open = bar_close - active_off — exactly Pine.
    No time delay needed. Zero artificial latency.

FIX-TRAIL-2 | Live ATR passed into on_bar_close() (Bug 2 fix)
  Root cause:
    risk.atr frozen at entry fill. Pine recalculates atr every bar.
    Trail offsets (activePts, activeOff) = atr * multiplier drift
    apart from Pine when ATR expands mid-trade.

  Fix:
    on_bar_close() accepts current_atr from snap.atr.
    risk.atr updated in-place every bar close.

FIX-TRAIL-3 | Breakeven also uses reset peak (Bug 3 fix)
  BE sets current_sl = entry_price at bar close.
  Peak reset to bar_close means tick loop won't immediately fire
  BE exit on stale intrabar price. Clean bar boundary, no delay.

PRESERVED FROM v3:
  PINE-FIX-001..008 | All prior fixes retained
──────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import time
from typing import Optional, Callable

import ccxt.async_support as ccxt

from risk.calculator import (
    RiskLevels, TrailState, calc_real_pl,
    max_sl_hit,
)
from config import (
    SYMBOL, DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    ALERT_QTY, TRAIL_SL_PRE_FIRE_BUFFER, TRAIL_LOOP_SEC,
    TRAIL_STAGES, BE_MULT,
    TREND_RR, RANGE_RR, TREND_ATR_MULT, RANGE_ATR_MULT, MAX_SL_POINTS,
)

logger = logging.getLogger(__name__)

# Block exits for 5s after entry fill — avoids noise on entry bar
ENTRY_GUARD_MS = 5 * 1000


# ─── Stage helpers ────────────────────────────────────────────────────────────

def _get_active_params(stage: int, atr: float):
    """Return (trail_points, trail_offset) for the given stage."""
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def _calc_new_stage(profit_dist: float, atr: float) -> int:
    """
    Return the highest stage whose trigger is satisfied by profit_dist.
    Mirrors Pine's if/else-if chain evaluated at bar close.
    """
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if profit_dist >= atr * trigger_mult:
            return i + 1
    return 0


def _compute_trail_sl(
    stage: int,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
    atr: float,
) -> Optional[float]:
    """
    Compute the current trail stop price.
    Returns None if stage == 0 (trail not yet active).
    Mirrors Pine: trail stop = peak - trail_offset  (long)
                              = peak + trail_offset  (short)
    """
    if stage == 0:
        return None
    _, active_pts, active_off = TRAIL_STAGES[stage - 1]
    active_pts_val = atr * active_pts
    active_off_val = atr * active_off
    if peak_profit_dist < active_pts_val:
        return None
    return (peak_price - active_off_val) if is_long else (peak_price + active_off_val)


# ─── TrailMonitor ─────────────────────────────────────────────────────────────

class TrailMonitor:
    """
    Tick-level exit monitor replicating Pine Script strategy.exit() exactly.

    Call flow:
        start()          — called once when position opens
        on_bar_close()   — called by main.py on every 30m bar close
        _on_tick()       — runs internally every TRAIL_LOOP_SEC seconds
        stop()           — called when position is closed
    """

    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal

        self.risk:  Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None

        self._running         = False
        self._task: Optional[asyncio.Task] = None
        self._exchange        = None
        self._exit_triggered  = False

        self.on_trail_exit: Optional[Callable] = None
        self.entry_bar_time_ms: Optional[int] = None

        self._active_pts: float = 0.0
        self._active_off: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels: RiskLevels,
        trail_state: TrailState,
        entry_bar_time_ms: Optional[int] = None,
        on_trail_exit: Optional[Callable] = None,
    ):
        self.risk             = risk_levels
        self.state            = trail_state
        self.on_trail_exit    = on_trail_exit
        self._exit_triggered  = False
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)

        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        self._active_pts, self._active_off = _get_active_params(0, risk_levels.atr)

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long}"
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    def on_bar_close(
        self,
        bar_close: float,
        bar_high: float,
        bar_low: float,
        current_atr: float,   # FIX-TRAIL-2: live ATR from current bar
    ):
        """
        Called by main.py at every 30-minute bar close.

        FIX-TRAIL-1: Resets peak_price = bar_close at end of every bar.
                     Tick loop starts the new bar tracking peak fresh
                     from bar_close — exactly like Pine's bar boundary.
                     Trail SL at bar open = bar_close - active_off.
                     No time delay. No guard window. Zero latency.

        FIX-TRAIL-2: Accepts current_atr, updates risk.atr in-place.
                     Pine recalculates atr every bar — trail offsets follow.

        FIX-TRAIL-3: Peak reset also neutralises stale-peak BE exit bug.
                     BE sets current_sl = entry_price at bar close.
                     Tick loop starts from bar_close, not intrabar peak,
                     so BE SL won't fire immediately on old price.
        """
        if not self._running or self.risk is None or self.state is None:
            return

        # FIX-TRAIL-2 + FIX-TRAIL-4: Recalculate ATR, SL, TP, stop_dist every bar.
        #
        # WHY THIS IS NEEDED (Pine parity):
        #   Pine Script recalculates on EVERY bar:
        #     stopDist = math.min(atr * atrMult, maxSLPoints)   ← new ATR each bar
        #     longSL   = entryPrice - stopDist                   ← shifts with ATR
        #     longTP   = entryPrice + stopDist * rr              ← shifts with ATR
        #     strategy.exit("Exit TL", stop=longSL, limit=longTP, ...)
        #   Because strategy.exit() is re-called each bar with fresh SL/TP,
        #   both levels drift as ATR expands or contracts over the trade lifetime.
        #
        #   Old code only updated risk.atr but kept risk.sl and risk.tp frozen at
        #   entry values — causing exits at completely different price levels vs Pine.
        #
        # SAFE UPDATE RULES:
        #   - risk.sl  : always updated (initial SL, Pine widens/tightens with ATR)
        #   - risk.tp  : always updated (Pine moves TP every bar)
        #   - state.current_sl: updated ONLY if trail has NOT yet moved it above
        #     the initial SL (i.e. trail is still at stage 0 or current_sl == old sl).
        #     Once trail/BE has ratcheted current_sl to a better level, we keep it —
        #     we never move current_sl backwards against the trade.
        old_atr      = self.risk.atr
        old_sl       = self.risk.sl
        atr_mult     = TREND_ATR_MULT if self.risk.is_trend else RANGE_ATR_MULT
        rr           = TREND_RR       if self.risk.is_trend else RANGE_RR
        new_stop_dist = min(current_atr * atr_mult, MAX_SL_POINTS)
        entry_price_  = self.risk.entry_price
        is_long_      = self.risk.is_long

        if is_long_:
            new_sl = entry_price_ - new_stop_dist
            new_tp = entry_price_ + new_stop_dist * rr
        else:
            new_sl = entry_price_ + new_stop_dist
            new_tp = entry_price_ - new_stop_dist * rr

        self.risk = RiskLevels(
            entry_price = entry_price_,
            sl          = new_sl,
            tp          = new_tp,
            stop_dist   = new_stop_dist,
            atr         = current_atr,
            is_long     = is_long_,
            is_trend    = self.risk.is_trend,
        )

        # Sync state.current_sl to new initial SL only if trail/BE hasn't
        # already moved it to a better (more profitable) level.
        # "Better" = above entry for long, below entry for short (BE/trail zone).
        trail_has_moved_sl = (
            (is_long_  and self.state.current_sl > old_sl + 1.0) or
            (not is_long_ and self.state.current_sl < old_sl - 1.0)
        )
        if not trail_has_moved_sl:
            self.state.current_sl = new_sl

        logger.info(
            f"[BAR CLOSE] ATR {old_atr:.2f}→{current_atr:.2f} | "
            f"stop_dist={new_stop_dist:.2f} | "
            f"sl={new_sl:.2f} tp={new_tp:.2f} | "
            f"current_sl={'kept at trail ' + str(round(self.state.current_sl,2)) if trail_has_moved_sl else str(round(new_sl,2))}"
        )

        risk, state = self.risk, self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # ── Stage upgrade uses bar CLOSE profit dist (Pine parity) ────────────
        close_profit_dist = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        close_profit_dist = max(0.0, close_profit_dist)

        new_stage = max(state.stage, _calc_new_stage(close_profit_dist, atr))
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            self._active_pts, self._active_off = _get_active_params(new_stage, atr)
            logger.info(
                f"[BAR CLOSE] Trail stage {old_stage}→{new_stage} "
                f"| close={bar_close:.2f} profit_dist={close_profit_dist:.2f} "
                f"active_off={self._active_off:.2f}"
            )
        else:
            self._active_pts, self._active_off = _get_active_params(state.stage, atr)

        # ── Breakeven at bar close (Pine parity) ──────────────────────────────
        if not state.be_done and close_profit_dist > atr * BE_MULT:
            state.be_done = True
            if (is_long and entry_price > state.current_sl) or \
               (not is_long and entry_price < state.current_sl):
                state.current_sl = entry_price
                logger.info(
                    f"[BAR CLOSE] Breakeven SL set to entry={entry_price:.2f} "
                    f"| close_profit_dist={close_profit_dist:.2f}"
                )

        # ── FIX-TRAIL-1: Reset peak to bar_close ──────────────────────────────
        # Pine's profitDist on any given bar = close - entryPrice (bar close).
        # The tick loop must start the new bar tracking peak from bar_close,
        # not from bar N's intrabar high/low. This is what prevents the
        # immediate false exit on stage upgrade — no guard window needed.
        state.peak_price = bar_close
        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak reset to bar_close={bar_close:.2f} "
            f"current_sl={state.current_sl:.2f} be_done={state.be_done} atr={atr:.2f}"
        )

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _run(self):
        _base_url = (
            "https://testnet-api.india.delta.exchange"
            if DELTA_TESTNET
            else "https://api.india.delta.exchange"
        )
        self._exchange = ccxt.delta({
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls"           : {"api": {"public": _base_url, "private": _base_url}},
        })
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if current_price > 0:
                        await self._on_tick(current_price)
                except Exception as e:
                    logger.warning(f"Tick fetch error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    async def _on_tick(self, current_price: float):
        if not self._running or self.risk is None or self.state is None:
            return

        risk, state   = self.risk, self.state
        now_ms        = int(time.time() * 1000)
        is_long       = risk.is_long
        entry_price   = risk.entry_price
        atr           = risk.atr

        # ── 1. Update intrabar peak from live tick ────────────────────────────
        # FIX-TRAIL-1: peak starts from bar_close (reset in on_bar_close).
        # From there tick loop grows it naturally through the new bar.
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # ── 2. Peak profit dist ───────────────────────────────────────────────
        peak_profit_dist = max(
            0.0,
            (state.peak_price - entry_price) if is_long
            else (entry_price - state.peak_price)
        )

        # ── 3. TP check ───────────────────────────────────────────────────────
        if not self._in_entry_guard(now_ms):
            if (is_long and current_price >= risk.tp) or \
               (not is_long and current_price <= risk.tp):
                logger.info(f"[TICK] Target Profit hit | price={current_price:.2f} tp={risk.tp:.2f}")
                await self._execute_exit(current_price, "Target Profit")
                return

        # ── 4. Max SL check ───────────────────────────────────────────────────
        if not self._in_entry_guard(now_ms) and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                logger.info(f"[TICK] Max SL hit | price={current_price:.2f}")
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # ── 5. Ratchet trail SL ───────────────────────────────────────────────
        if state.stage > 0:
            trail_sl = _compute_trail_sl(
                state.stage, state.peak_price, peak_profit_dist, is_long, atr
            )
            if trail_sl is not None:
                if (is_long and trail_sl > state.current_sl) or \
                   (not is_long and trail_sl < state.current_sl):
                    state.current_sl = trail_sl

        # ── 6. SL check ───────────────────────────────────────────────────────
        if state.current_sl > 0 and not self._in_entry_guard(now_ms):
            sl_hit = (
                (is_long  and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_hit:
                trail_active = (
                    (is_long  and state.current_sl > risk.sl) or
                    (not is_long and state.current_sl < risk.sl)
                )
                if state.be_done and abs(state.current_sl - entry_price) < 1.0:
                    reason = "Breakeven SL"
                elif trail_active:
                    reason = f"Trail S{state.stage}"
                else:
                    reason = "Initial SL"

                logger.info(
                    f"[TICK] {reason} hit | price={current_price:.2f} "
                    f"sl={state.current_sl:.2f} stage={state.stage}"
                )
                await self._execute_exit(current_price, reason)

    # ── Guards ────────────────────────────────────────────────────────────────

    def _in_entry_guard(self, now_ms: int) -> bool:
        if self.entry_bar_time_ms is None:
            return False
        return (now_ms - self.entry_bar_time_ms) < ENTRY_GUARD_MS

    # ── Exit execution ────────────────────────────────────────────────────────

    async def _execute_exit(self, price: float, reason: str):
        if self._exit_triggered:
            return
        self._exit_triggered = True
        self._running = False

        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(
                exit_order.get("average") or exit_order.get("price") or price
            )
        except Exception as e:
            logger.error(f"close_at_trail_sl failed: {e}")
            fill_price = price

        logger.info(
            f"Exit executed | reason={reason} "
            f"price={price:.2f} fill={fill_price:.2f}"
        )

        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)
