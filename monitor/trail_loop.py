"""
monitor/trail_loop.py - Shiva Sniper v6.5-30M
TV-Exact Execution Logic (Includes FIX-007 Same-Bar Guard & Timestamp Alignment)
"""

import asyncio
import logging
import time
from typing import Optional
from datetime import datetime, timezone
import ccxt.async_support as ccxt

from risk.calculator import (
    TrailState,
    RiskLevels,
    calc_trail_stage,
    get_trail_params,
    should_trigger_be,
    max_sl_hit,
    calc_real_pl,
)
from config import (
    ALERT_QTY,
    SYMBOL,
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_TESTNET,
    BE_MULT,
    TRAIL_SL_PRE_FIRE_BUFFER,
)

logger = logging.getLogger(__name__)

FIXED_TRAIL_LOOP_SEC = 0.5
BAR_PERIOD_MS = 1800000  # 30 minutes in milliseconds
TV_MINTICK = 0.5         # Delta India BTC/USD tick size

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr  = order_manager
        self.telegram   = telegram
        self.journal    = journal
        self.risk:  Optional[RiskLevels]  = None
        self.state: Optional[TrailState] = None
        self._running   = False
        self._task: Optional[asyncio.Task] = None
        self._exchange: Optional[ccxt.delta] = None
        self.on_trail_exit = None
        self.entry_bar_boundary: int = 0

    def start(self, risk_levels: RiskLevels, trail_state: TrailState, on_trail_exit=None) -> None:
        self.risk  = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit
        
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price
            
        # FIX-007: Record the 30m boundary of the entry bar to block Max SL
        current_time_ms = int(time.time() * 1000)
        self.entry_bar_boundary = (current_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
            
        self._running = True
        self._task    = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

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
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public": _base_url, "private": _base_url}},
        }
        self._exchange = ccxt.delta(params)
        try:
            while self._running:
                await asyncio.sleep(FIXED_TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(ticker.get("last") or ticker.get("info", {}).get("mark_price") or 0)
                    if current_price > 0:
                        await self._on_tick(current_price)
                except Exception as e:
                    logger.error(f"Tick processing error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    async def _on_tick(self, current_price: float) -> None:
        if not self._running or self.risk is None or self.state is None:
            return

        risk        = self.risk
        state       = self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        peak_profit_dist    = abs(state.peak_price - entry_price)
        current_profit_dist = abs(current_price    - entry_price)

        # ── 1. Target Profit (TP) ─────────────────
        tp_hit = (is_long and current_price >= risk.tp) or (not is_long and current_price <= risk.tp)
        if tp_hit:
            await self._execute_exit(current_price, "Target Profit")
            return

        # ── 2. Trail Stop Execution ─────────────────
        if state.current_sl > 0:
            sl_breached = (
                (is_long     and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_breached:
                reason = "Initial SL" if (state.stage == 0 and not state.be_done) else f"Trail S{max(1, state.stage)}"
                await self._execute_exit(current_price, reason)
                return

        # ── 3. FIX-007: Max SL with Same-Bar Guard ─────────────────
        current_time_ms = int(time.time() * 1000)
        current_bar_boundary = (current_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        is_entry_bar = (current_bar_boundary == self.entry_bar_boundary)

        if not is_entry_bar and not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
            state.max_sl_fired = True
            await self._execute_exit(current_price, "Max SL Hit")
            return

        # ── 4. Breakeven Execution ─────────────────
        if not state.be_done and should_trigger_be(current_profit_dist, atr):
            state.be_done = True
            if self._sl_improved(entry_price, state.current_sl, is_long):
                state.current_sl = entry_price
            await self.telegram.notify_breakeven(entry_price)

        # ── 5. Trail Stage Advancement ─────────────────
        new_stage = calc_trail_stage(peak_profit_dist, atr)
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            await self.telegram.notify_trail_stage(old_stage, state.stage, current_price, state.current_sl)

        # ── 6. Trail Stop Ratchet (Pine Script Parity) ─────────────────
        # Note: Even if stage == 0, PineScript defaults to stage 1 points/offset for initial trail calculation.
        raw_pts, raw_off = get_trail_params(state.stage, atr)
        activation_profit = raw_pts * TV_MINTICK
        trail_distance    = raw_off * TV_MINTICK

        if peak_profit_dist >= activation_profit:
            candidate_sl = (state.peak_price - trail_distance) if is_long else (state.peak_price + trail_distance)

            if self._sl_improved(candidate_sl, state.current_sl, is_long):
                state.current_sl = candidate_sl
                logger.info(
                    f"TRAIL SL update | stage={max(1, state.stage)} "
                    f"peak={state.peak_price:.2f} "
                    f"act={activation_profit:.2f} "
                    f"dist={trail_distance:.2f} "
                    f"new_sl={candidate_sl:.2f}"
                )
                self.journal.update_open_trade(
                    trail_stage=max(1, state.stage),
                    current_sl=state.current_sl,
                    peak_price=state.peak_price,
                )

    async def _execute_exit(self, price: float, reason: str):
        self._running = False
        
        # Calculate TV-Aligned Timestamp for Logging Parity
        current_time_ms = int(time.time() * 1000)
        current_bar_boundary = (current_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        tv_time_str = datetime.fromtimestamp(current_bar_boundary / 1000, tz=timezone.utc).strftime('%b %d, %Y, %H:%M')
        
        logger.info(f"TV-Aligned Exit Time: {tv_time_str} | Real Execution: {datetime.now(timezone.utc).strftime('%H:%M:%S')}")

        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            exit_price = float(exit_order.get("average") or exit_order.get("price") or price)
        except Exception as e:
            logger.error(f"Exit failed: {e}")
            exit_price = price

        real_pl = calc_real_pl(self.risk.entry_price, exit_price, self.risk.is_long, ALERT_QTY)
        await self.telegram.notify_exit(reason, self.risk.entry_price, exit_price, real_pl)
        if self.on_trail_exit:
            await self.on_trail_exit(exit_price, reason)

    @staticmethod
    def _sl_improved(new_sl: float, current_sl: float, is_long: bool) -> bool:
        if current_sl == 0.0:
            return True
        return new_sl > current_sl if is_long else new_sl < current_sl
