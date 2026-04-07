"""
monitor/trail_loop.py - Shiva Sniper v6.5
Final Sync Version: 0.5s Polling + Last Price + TP/SL Fixes
"""

import asyncio
import logging
from typing import Optional

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
)

logger = logging.getLogger(__name__)

# User requested 0.5s delay to replace the default 2.0s
FIXED_TRAIL_LOOP_SEC = 0.5

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

    def start(self, risk_levels: RiskLevels, trail_state: TrailState,
              on_trail_exit=None) -> None:
        self.risk  = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price
        self._running = True
        self._task    = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long}"
        )

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
        _base_url = ("https://testnet-api.india.delta.exchange"
                     if DELTA_TESTNET else
                     "https://api.india.delta.exchange")
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {"api": {"public" : _base_url, "private": _base_url}},
        }
        self._exchange = ccxt.delta(params)
        logger.info(f"Trail ticker polling every {FIXED_TRAIL_LOOP_SEC}s for {SYMBOL}")

        try:
            while self._running:
                await asyncio.sleep(FIXED_TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    # FIX: Prioritize 'last' price to match TradingView charts exactly
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

        risk  = self.risk
        state = self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # ── 1. Update peak price (High/Low tracker) ──────────────────────────
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # ── 2. Calculate Distances ───────────────────────────────────────────
        # Pine Stage triggers use 'close' (current_price)
        profit_dist_for_triggers = abs(current_price - entry_price)
        # Trail SL placement uses 'peak_price'
        profit_dist_for_peak = abs(state.peak_price - entry_price)

        # ── 3. Target Profit (TP) Check ──────────────────────────────────────
        tp_hit = (is_long and current_price >= risk.tp) or (not is_long and current_price <= risk.tp)
        if tp_hit:
            await self._execute_exit(current_price, "Target Profit")
            return

        # ── 4. Stop Loss Check (Initial + Trailing) ──────────────────────────
        # Fixed: Removed the 'stage > 0' gate so the initial SL is always active
        if state.current_sl > 0:
            sl_breached = (is_long and current_price <= state.current_sl) or (not is_long and current_price >= state.current_sl)
            if sl_breached:
                reason = "Initial SL" if (state.stage == 0 and not state.be_done) else f"Trail S{state.stage}"
                await self._execute_exit(current_price, reason)
                return

        # ── 5. Max SL (Emergency) ────────────────────────────────────────────
        if not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
            state.max_sl_fired = True
            await self._execute_exit(current_price, "Max SL Hit")
            return

        # ── 6. Breakeven ──────────────────────────────────────────────────────
        if not state.be_done and should_trigger_be(profit_dist_for_triggers, atr):
            state.be_done    = True
            state.current_sl = entry_price
            await self.telegram.notify_breakeven(entry_price)

        # ── 7. Trail Ratchet ──────────────────────────────────────────────────
        new_stage = calc_trail_stage(profit_dist_for_triggers, atr)
        if new_stage > state.stage:
            state.stage = new_stage
            await self.telegram.notify_trail_stage(state.stage - 1, state.stage, current_price, state.current_sl)

        if state.stage > 0:
            trail_pts, _ = get_trail_params(state.stage, atr)
            candidate_sl = (state.peak_price - trail_pts) if is_long else (state.peak_price + trail_pts)

            if self._sl_improved(candidate_sl):
                state.current_sl = candidate_sl
                # Journal sync for dashboard
                self.journal.update_open_trade(trail_stage=state.stage, current_sl=state.current_sl, peak_price=state.peak_price)

    async def _execute_exit(self, price: float, reason: str):
        self._running = False
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

    def _sl_improved(self, new_sl: float) -> bool:
        if not self.risk or not self.state: return False
        return new_sl > self.state.current_sl if self.risk.is_long else new_sl < self.state.current_sl
