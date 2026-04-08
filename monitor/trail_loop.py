"""
monitor/trail_loop.py - Shiva Sniper v6.5
TRUE Pine Script Parity with TV-Sync Exit Timing
"""

import asyncio
import logging
import time
from typing import Optional, Callable
from dataclasses import dataclass
import ccxt.async_support as ccxt

from risk.calculator import (
    RiskLevels, TrailState, calc_trail_stage, get_trail_params,
    should_trigger_be, max_sl_hit, calc_real_pl, TRAIL_STAGES
)
from config import (
    SYMBOL, DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    ALERT_QTY, BE_MULT, TRAIL_SL_PRE_FIRE_BUFFER, TRAIL_LOOP_SEC,
    TV_SYNC_MODE
)

logger = logging.getLogger(__name__)

BAR_PERIOD_MS = 30 * 60 * 1000  # 30 minutes

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram = telegram
        self.journal = journal
        self.tv_sync = TV_SYNC_MODE
        
        self.risk: Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._exchange: Optional[ccxt.delta] = None
        self.on_trail_exit: Optional[Callable] = None
        
        self.entry_bar_time_ms: Optional[int] = None
        self.pending_exit: Optional[tuple] = None

    def _get_bar_boundary(self, timestamp_ms: Optional[int] = None) -> int:
        """Get next 30m bar close time (TV reporting standard)"""
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        return ((timestamp_ms // BAR_PERIOD_MS) + 1) * BAR_PERIOD_MS

    def _is_same_bar(self, timestamp_ms: int) -> bool:
        """Check if timestamp is within same bar as entry (FIX-007)"""
        if self.entry_bar_time_ms is None:
            return False
        return (timestamp_ms // BAR_PERIOD_MS) == (self.entry_bar_time_ms // BAR_PERIOD_MS)

    def start(self, risk_levels: RiskLevels, trail_state: TrailState, 
             entry_bar_time_ms: Optional[int] = None,
             on_trail_exit: Optional[Callable] = None) -> None:
        self.risk = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit
        self.pending_exit = None
        
        if entry_bar_time_ms:
            self.entry_bar_time_ms = entry_bar_time_ms
        else:
            self.entry_bar_time_ms = self._get_bar_boundary()
            
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price
            
        logger.info(
            f"TrailMonitor started [TV-Sync={self.tv_sync}] | "
            f"entry={risk_levels.entry_price} atr={risk_levels.atr} "
            f"entry_bar={self.entry_bar_time_ms}"
        )
        
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("TrailMonitor stopped.")

    async def _run(self) -> None:
        try:
            await self._loop_rest()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TrailMonitor crashed: {e}", exc_info=True)

    async def _loop_rest(self) -> None:
        _base_url = "https://testnet-api.india.delta.exchange" if DELTA_TESTNET else "https://api.india.delta.exchange"
        params = {
            "apiKey": DELTA_API_KEY,
            "secret": DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": _base_url, "private": _base_url}},
        }
        self._exchange = ccxt.delta(params)
        
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(ticker.get("last") or 
                                        ticker.get("info", {}).get("mark_price") or 0)
                    if current_price > 0:
                        await self._on_tick(current_price)
                except Exception as e:
                    logger.error(f"Tick processing error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    async def _on_tick(self, current_price: float) -> None:
        """Process price tick with true Pine Script logic"""
        if not self._running or self.risk is None or self.state is None:
            return

        risk = self.risk
        state = self.state
        is_long = risk.is_long
        entry_price = risk.entry_price
        atr = risk.atr
        current_time_ms = int(time.time() * 1000)

        # Update peak price
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        peak_profit_dist = abs(state.peak_price - entry_price)
        current_profit_dist = abs(current_price - entry_price)

        # 1. Check Target Profit
        tp_hit = (is_long and current_price >= risk.tp) or (not is_long and current_price <= risk.tp)
        if tp_hit:
            await self._schedule_exit(current_price, "Target Profit", current_time_ms)
            return

        # 2. Check Max SL (BLOCKED on entry bar - FIX-007)
        is_entry_bar = self._is_same_bar(current_time_ms)
        if not is_entry_bar and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                await self._schedule_exit(current_price, "Max SL Hit", current_time_ms)
                return

        # 3. Check Breakeven
        if not state.be_done and should_trigger_be(current_profit_dist, atr):
            state.be_done = True
            if self._sl_improves(entry_price, state.current_sl, is_long):
                old_sl = state.current_sl
                state.current_sl = entry_price
                logger.info(f"Breakeven | SL {old_sl:.2f} → {entry_price:.2f}")
                await self.telegram.notify_breakeven(entry_price)

        # 4. Update Trail Stage based on PEAK profit
        new_stage = calc_trail_stage(peak_profit_dist, atr)
        if new_stage > state.stage:
            old_stage = state.stage
            state.stage = new_stage
            pts, _ = get_trail_params(state.stage, atr)
            logger.info(
                f"TRAIL stage {old_stage} → {state.stage} | "
                f"peak={state.peak_price:.2f} pts={pts:.2f}"
            )
            await self.telegram.notify_trail_stage(old_stage, state.stage, current_price, state.current_sl)

        # 5. Calculate Trailing Stop
        if state.stage > 0:
            pts, off = get_trail_params(state.stage, atr)
            
            # Activation threshold from TRAIL_STAGES
            activation_threshold = TRAIL_STAGES[max(0, state.stage-1)][0] * atr
            
            if peak_profit_dist >= activation_threshold:
                if is_long:
                    candidate_sl = state.peak_price - pts
                else:
                    candidate_sl = state.peak_price + pts
                    
                if self._sl_improves(candidate_sl, state.current_sl, is_long):
                    old_sl = state.current_sl
                    state.current_sl = candidate_sl
                    logger.info(
                        f"TRAIL SL update | stage={state.stage} "
                        f"peak={state.peak_price:.2f} pts={pts:.2f} "
                        f"SL {old_sl:.2f} → {candidate_sl:.2f}"
                    )

        # 6. Check Trail Stop hit
        if state.current_sl > 0 and state.stage > 0:
            sl_hit = (
                (is_long and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_hit:
                await self._schedule_exit(current_price, f"Trail S{state.stage}", current_time_ms)
                return

        # 7. Check Initial SL
        if state.stage == 0:
            initial_sl_hit = (
                (is_long and current_price <= risk.sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= risk.sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if initial_sl_hit:
                await self._schedule_exit(current_price, "Initial SL", current_time_ms)
                return

    async def _schedule_exit(self, price: float, reason: str, detected_time_ms: int):
        """Schedule exit with TV time sync"""
        if not self.tv_sync:
            await self._execute_exit(price, reason, detected_time_ms)
            return
            
        exit_bar_time = self._get_bar_boundary(detected_time_ms)
        
        if self.pending_exit is None:
            self.pending_exit = (price, reason, detected_time_ms, exit_bar_time)
            logger.info(
                f"Exit scheduled | reason={reason} price={price:.2f} "
                f"at_bar={exit_bar_time}"
            )
            
            wait_ms = exit_bar_time - detected_time_ms
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)
                
            if self._running and self.pending_exit:
                await self._execute_exit(price, reason, exit_bar_time)

    async def _execute_exit(self, price: float, reason: str, timestamp_ms: int):
        """Execute exit with TV-aligned timestamp"""
        self._running = False
        
        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(exit_order.get("average") or exit_order.get("price") or price)
        except Exception as e:
            logger.error(f"Exit failed: {e}")
            fill_price = price

        real_pl = calc_real_pl(self.risk.entry_price, fill_price, self.risk.is_long, ALERT_QTY)
        
        bar_time_str = time.strftime('%Y-%m-%d %H:%M', time.gmtime(timestamp_ms / 1000))
        logger.info(
            f"Position closed | exit={fill_price:.2f} reason={reason} "
            f"pl={real_pl:.4f} tv_time={bar_time_str}"
        )
        
        await self.telegram.notify_exit(reason, self.risk.entry_price, fill_price, real_pl)
        
        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)

    @staticmethod
    def _sl_improves(new_sl: float, current_sl: float, is_long: bool) -> bool:
        if current_sl == 0.0:
            return True
        return new_sl > current_sl if is_long else new_sl < current_sl
