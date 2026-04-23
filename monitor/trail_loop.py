"""
monitor/trail_loop.py — Shiva Sniper v6.5 (PINE-EXACT ALIGNED)

═══════════════════════════════════════════════════════════════════════
LOGIC PARITY FIXES (Aligning Bot exits to Pine Script)
═══════════════════════════════════════════════════════════════════════
1. Breakeven (BE) Timing: Removed tick-level BE evaluation. Pine
   evaluates BE strictly on the closing price of a completed bar.
2. Max SL Timing & Entry Block: Removed tick-level Max SL evaluation.
   Pine evaluates Max SL strictly at bar close, and explicitly uses 
   `blockExitMaxSL` to ensure it never fires on the entry bar itself.
═══════════════════════════════════════════════════════════════════════
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

ENTRY_GUARD_MS = 0
S0_ACTIVATION_ATR_MULTIPLE = 0.5

# ─── Stage helpers ────────────────────────────────────────────────────────────

def _get_active_params(stage: int, atr: float):
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult

def _calc_new_stage(profit_dist: float, atr: float) -> int:
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
    _, pts_mult, off_mult = TRAIL_STAGES[max(stage - 1, 0)]
    if peak_profit_dist < atr * pts_mult:
        return None
    trail_dist = atr * off_mult
    return (peak_price - trail_dist) if is_long else (peak_price + trail_dist)


# ─── TrailMonitor ─────────────────────────────────────────────────────────────

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal

        self.risk:  Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None

        self._running        = False
        self._task: Optional[asyncio.Task] = None
        self._exchange: Optional[ccxt.delta] = None   
        self._exit_triggered = False

        self.on_trail_exit: Optional[Callable] = None
        self.entry_bar_time_ms: Optional[int] = None

        self._active_pts: float = 0.0
        self._active_off: float = 0.0
        self._bar_just_closed: bool = False
        
        # FIX: Track the first bar close to mirror Pine's `blockExitMaxSL` logic
        self._is_first_bar_close: bool = True

    # ── Public API ────────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels: RiskLevels,
        trail_state: TrailState,
        entry_bar_time_ms: Optional[int] = None,
        on_trail_exit: Optional[Callable] = None,
    ):
        self.risk            = risk_levels
        self.state           = trail_state
        self.on_trail_exit   = on_trail_exit
        self._exit_triggered = False
        self._bar_just_closed = False
        self._is_first_bar_close = True  # Reset on new trade
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)

        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        self._active_pts, self._active_off = _get_active_params(0, risk_levels.atr)
        self._running = True

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

        self._exchange.markets = self.order_mgr.exchange.markets
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long} "
            f"poll_interval={TRAIL_LOOP_SEC}s [ENTRY_GUARD_MS={ENTRY_GUARD_MS}]"
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self._exchange:
            asyncio.create_task(self._close_exchange())

    # ── WS intrabar peak injection ────────────────────────────────────────────

    def update_intrabar_high(self, high: float) -> None:
        if not self._running or self.state is None or self.risk is None:
            return
        if self.risk.is_long and high > self.state.peak_price:
            self.state.peak_price = high
            logger.debug(f"[WS-PEAK] peak_price updated → {high:.2f} (long)")

    def update_intrabar_low(self, low: float) -> None:
        if not self._running or self.state is None or self.risk is None:
            return
        if not self.risk.is_long and low < self.state.peak_price:
            self.state.peak_price = low
            logger.debug(f"[WS-PEAK] peak_price updated → {low:.2f} (short)")

    async def _close_exchange(self):
        try:
            if self._exchange:
                await self._exchange.close()
                self._exchange = None
        except Exception as e:
            logger.debug(f"Exchange close: {e}")

    def on_bar_close(
        self,
        bar_close: float,
        bar_high: float,
        bar_low: float,
        current_atr: float,
    ):
        if not self._running or self.risk is None or self.state is None:
            return

        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

        self._bar_just_closed = True
        is_entry_bar = self._is_first_bar_close
        self._is_first_bar_close = False

        old_atr       = self.risk.atr
        old_sl        = self.risk.sl
        atr_mult      = TREND_ATR_MULT if self.risk.is_trend else RANGE_ATR_MULT
        rr            = TREND_RR       if self.risk.is_trend else RANGE_RR
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

        trail_has_moved_sl = (
            (is_long_  and self.state.current_sl > old_sl + 1.0) or
            (not is_long_ and self.state.current_sl < old_sl - 1.0)
        )
        if not trail_has_moved_sl:
            if is_long_:
                self.state.current_sl = max(new_sl, self.state.current_sl)
            else:
                self.state.current_sl = min(new_sl, self.state.current_sl)

        logger.info(
            f"[BAR CLOSE] ATR {old_atr:.2f}→{current_atr:.2f} | "
            f"stop_dist={new_stop_dist:.2f} | "
            f"sl={new_sl:.2f} tp={new_tp:.2f}(pine-recalc) | "
            f"current_sl={'trail=' + str(round(self.state.current_sl,2)) if trail_has_moved_sl else str(round(new_sl,2))}"
        )

        risk, state = self.risk, self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

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

        # ── FIX: BREAKEVEN ALIGNED TO PINE BAR CLOSE ──
        if not state.be_done and close_profit_dist > atr * BE_MULT:
            state.be_done = True
            if (is_long and entry_price > state.current_sl) or \
               (not is_long and entry_price < state.current_sl):
                state.current_sl = entry_price
                logger.info(
                    f"[BAR CLOSE] Breakeven SL set to entry={entry_price:.2f} "
                    f"| close_profit_dist={close_profit_dist:.2f}"
                )

        if is_long:
            state.peak_price = max(state.peak_price, bar_high, bar_close)
        else:
            state.peak_price = min(state.peak_price, bar_low, bar_close)

        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak={state.peak_price:.2f} "
            f"(bar_high={bar_high:.2f} bar_low={bar_low:.2f} bar_close={bar_close:.2f}) "
            f"current_sl={state.current_sl:.2f} be_done={state.be_done} atr={atr:.2f}"
        )

        bar_exited = False
        
        # ── FIX: MAX SL ALIGNED TO PINE BAR CLOSE ──
        # Evaluates strictly on candle close and blocks firing on the entry candle
        if not is_entry_bar and not state.max_sl_fired:
            if max_sl_hit(bar_close, entry_price, atr, is_long):
                state.max_sl_fired = True
                logger.info(f"[BAR CLOSE] Max SL hit on close={bar_close:.2f}")
                asyncio.create_task(self._execute_exit(bar_close, "Max SL Hit"))
                bar_exited = True

        if self._running and not self._exit_triggered and not bar_exited:
            tp_ = risk.tp
            sl_ = state.current_sl
            if is_long:
                if bar_high >= tp_:
                    asyncio.create_task(self._execute_exit(bar_high, "Target Profit"))
                    bar_exited = True
                elif bar_low <= sl_ + TRAIL_SL_PRE_FIRE_BUFFER:
                    trail_active = state.current_sl > risk.sl
                    if state.be_done and abs(state.current_sl - entry_price) < 1.0:
                        sl_reason = "Breakeven SL"
                    elif trail_active:
                        sl_reason = f"Trail S{state.stage}"
                    else:
                        sl_reason = "Initial SL"
                    asyncio.create_task(self._execute_exit(bar_low, sl_reason))
                    bar_exited = True
            else:
                if bar_low <= tp_:
                    asyncio.create_task(self._execute_exit(bar_low, "Target Profit"))
                    bar_exited = True
                elif bar_high >= sl_ - TRAIL_SL_PRE_FIRE_BUFFER:
                    trail_active = state.current_sl < risk.sl
                    if state.be_done and abs(state.current_sl - entry_price) < 1.0:
                        sl_reason = "Breakeven SL"
                    elif trail_active:
                        sl_reason = f"Trail S{state.stage}"
                    else:
                        sl_reason = "Initial SL"
                    asyncio.create_task(self._execute_exit(bar_high, sl_reason))
                    bar_exited = True

        if self._running and not bar_exited:
            self._task = asyncio.create_task(self._run())
            logger.debug("[BAR CLOSE] Tick task restarted (exchange reused — no reconnect)")

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _run(self):
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                if not self._running:
                    break
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if current_price > 0:
                        await self._on_tick(current_price)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Tick fetch error: {e}")
        except asyncio.CancelledError:
            pass  

    async def _on_tick(self, current_price: float):
        if not self._running or self.risk is None or self.state is None:
            return

        risk, state = self.risk, self.state
        now_ms      = int(time.time() * 1000)
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # 1. Update intrabar peak
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # 2. Peak profit dist (from peak to entry)
        peak_profit_dist = max(
            0.0,
            (state.peak_price - entry_price) if is_long
            else (entry_price - state.peak_price)
        )

        # 3. TP check — Pine executes TP intrabar via Strategy Tester limit orders.
        if (is_long and current_price >= risk.tp) or \
           (not is_long and current_price <= risk.tp):
            logger.info(f"[TICK] Target Profit hit | price={current_price:.2f} tp={risk.tp:.2f}")
            await self._execute_exit(current_price, "Target Profit")
            return

        # 4. Ratchet trail SL — Pine trails natively intrabar.
        trail_sl = _compute_trail_sl(
            state.stage, state.peak_price, peak_profit_dist, is_long, atr
        )
        if trail_sl is not None:
            if (is_long and trail_sl > state.current_sl) or \
               (not is_long and trail_sl < state.current_sl):
                state.current_sl = trail_sl
                logger.debug(
                    f"[TICK] Trail SL ratcheted → {state.current_sl:.2f} "
                    f"(peak={state.peak_price:.2f} peak_profit={peak_profit_dist:.2f} "
                    f"stage={state.stage})"
                )

        # 5. SL check — Pine executes standard strategy.exit SL intrabar.
        if state.current_sl > 0:
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
