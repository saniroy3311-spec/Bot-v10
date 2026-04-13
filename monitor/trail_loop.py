"""
monitor/trail_loop.py - Shiva Sniper v6.5 (FIX-EXIT-v2)
Tick-level exit monitor — exact replica of Pine Script strategy.exit() mechanic.
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
)

logger = logging.getLogger(__name__)

ENTRY_GUARD_MS  = 30 * 1000  # Block exits for 30s after fill to avoid noise

def _get_active_params(stage: int, atr: float):
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult

def _upgrade_stage(current_stage: int, peak_profit_dist: float, atr: float) -> int:
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if peak_profit_dist >= atr * trigger_mult:
            new_stage = max(new_stage, i + 1)
            break
    return new_stage

def _compute_trail_sl(stage: int, peak_price: float, peak_profit_dist: float, is_long: bool, atr: float) -> Optional[float]:
    active_pts, active_off = _get_active_params(stage, atr)
    if peak_profit_dist < active_pts:
        return None
    return (peak_price - active_off) if is_long else (peak_price + active_off)

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal
        self.risk: Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._exchange = None
        self.on_trail_exit: Optional[Callable] = None
        self.entry_bar_time_ms: Optional[int] = None
        self._exit_triggered = False

    def _in_entry_guard(self, now_ms: int) -> bool:
        if self.entry_bar_time_ms is None: return False
        return (now_ms - self.entry_bar_time_ms) < ENTRY_GUARD_MS

    def start(self, risk_levels: RiskLevels, trail_state: TrailState, entry_bar_time_ms: Optional[int] = None, on_trail_exit: Optional[Callable] = None):
        self.risk = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit
        self._exit_triggered = False
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)
        if self.state.peak_price == 0.0: self.state.peak_price = risk_levels.entry_price
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task: self._task.cancel()

    async def _run(self):
        _base_url = "https://testnet-api.india.delta.exchange" if DELTA_TESTNET else "https://api.india.delta.exchange"
        self._exchange = ccxt.delta({"apiKey": DELTA_API_KEY, "secret": DELTA_API_SECRET, "enableRateLimit": True, "urls": {"api": {"public": _base_url, "private": _base_url}}})
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                ticker = await self._exchange.fetch_ticker(SYMBOL)
                current_price = float(ticker.get("last") or ticker.get("info", {}).get("mark_price") or 0)
                if current_price > 0: await self._on_tick(current_price)
        finally:
            if self._exchange: await self._exchange.close()

    async def _on_tick(self, current_price: float):
        if not self._running or self.risk is None or self.state is None: return
        risk, state, now_ms = self.risk, self.state, int(time.time() * 1000)
        is_long, entry_price, atr = risk.is_long, risk.entry_price, risk.atr

        # 1. Update peak
        if is_long: state.peak_price = max(state.peak_price, current_price)
        else: state.peak_price = min(state.peak_price, current_price)

        # 2. Profit Distances
        peak_profit_dist = max(0.0, (state.peak_price - entry_price) if is_long else (entry_price - state.peak_price))
        current_profit_dist = (current_price - entry_price) if is_long else (entry_price - current_price)

        # 3. Target Profit / Max SL
        if not self._in_entry_guard(now_ms):
            if (is_long and current_price >= risk.tp) or (not is_long and current_price <= risk.tp):
                await self._execute_exit(current_price, "Target Profit")
                return
            if not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # 4. Breakeven / Stage Upgrade
        if not state.be_done and current_profit_dist > atr * BE_MULT:
            state.be_done = True
            if (is_long and entry_price > state.current_sl) or (not is_long and entry_price < state.current_sl):
                state.current_sl = entry_price
                await self.telegram.notify_breakeven(entry_price)

        new_stage = _upgrade_stage(state.stage, peak_profit_dist, atr)
        if new_stage > state.stage:
            old_stage, state.stage = state.stage, new_stage
            await self.telegram.notify_trail_stage(old_stage, state.stage, current_price, state.current_sl)

        # 5. Trail Ratchet
        trail_sl = _compute_trail_sl(state.stage, state.peak_price, peak_profit_dist, is_long, atr)
        if trail_sl is not None:
            if (is_long and trail_sl > state.current_sl) or (not is_long and trail_sl < state.current_sl):
                state.current_sl = trail_sl

        # 6. SL Check (FIX-EXIT-v2: All SL types evaluated intra-bar)
        if state.current_sl > 0 and not self._in_entry_guard(now_ms):
            sl_hit = (is_long and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or \
                     (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            if sl_hit:
                trail_active = (is_long and state.current_sl > risk.sl) or (not is_long and state.current_sl < risk.sl)
                reason = f"Trail S{state.stage}" if trail_active else "Initial SL"
                if state.be_done and abs(state.current_sl - entry_price) < 1.0: reason = "Breakeven SL"
                await self._execute_exit(current_price, reason)

    async def _execute_exit(self, price: float, reason: str):
        if self._exit_triggered: return
        self._exit_triggered = True
        self._running = False
        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(exit_order.get("average") or exit_order.get("price") or price)
        except Exception: fill_price = price

        real_pl = calc_real_pl(self.risk.entry_price, fill_price, self.risk.is_long, ALERT_QTY)
        await self.telegram.notify_exit(reason, self.risk.entry_price, fill_price, real_pl, qty=ALERT_QTY, is_long=self.risk.is_long)
        if self.on_trail_exit: await self.on_trail_exit(fill_price, reason)
