"""
monitor/trail_loop.py - Shiva Sniper v6.5
FIXED: Exits fire IMMEDIATELY when trail SL / TP / Max SL is hit.
       TV_SYNC_MODE bar-wait removed — Pine Script exits intra-bar,
       the bot must do the same.

Changes vs original:
  FIX-TV-001 | _schedule_exit replaced with direct _execute_exit.
              | No more bar-boundary wait. Exit happens NOW.
  FIX-TV-002 | _exit_triggered boolean guard prevents double-exit.
  FIX-TV-003 | Logging shows real wall-clock time (not bar boundary).
"""

import asyncio
import logging
import time
from typing import Optional, Callable
import ccxt.async_support as ccxt

from risk.calculator import (
    RiskLevels, TrailState, calc_trail_stage, get_trail_params,
    should_trigger_be, max_sl_hit, calc_real_pl
)
from config import (
    SYMBOL, DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    ALERT_QTY, TRAIL_SL_PRE_FIRE_BUFFER, TRAIL_LOOP_SEC,
)

logger = logging.getLogger(__name__)

BAR_PERIOD_MS = 30 * 60 * 1000  # 30 minutes


class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram = telegram
        self.journal = journal

        self.risk: Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._exchange = None
        self.on_trail_exit: Optional[Callable] = None

        self.entry_bar_time_ms: Optional[int] = None
        # FIX-TV-002: guard against double-exit
        self._exit_triggered = False

    # ──────────────────────────────────────────────
    # Bar helpers (kept for entry-bar FIX-007 guard only)
    # ──────────────────────────────────────────────
    def _is_same_bar(self, timestamp_ms: int) -> bool:
        if self.entry_bar_time_ms is None:
            return False
        return (timestamp_ms // BAR_PERIOD_MS) == (self.entry_bar_time_ms // BAR_PERIOD_MS)

    # ──────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────
    def start(self, risk_levels: RiskLevels, trail_state: TrailState,
              entry_bar_time_ms: Optional[int] = None,
              on_trail_exit: Optional[Callable] = None) -> None:
        self.risk = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit
        self._exit_triggered = False

        self.entry_bar_time_ms = entry_bar_time_ms if entry_bar_time_ms else int(time.time() * 1000)

        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        logger.info(
            f"TrailMonitor started | "
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
        _base_url = (
            "https://testnet-api.india.delta.exchange"
            if DELTA_TESTNET
            else "https://api.india.delta.exchange"
        )
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
                    current_price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if current_price > 0:
                        await self._on_tick(current_price)
                except Exception as e:
                    logger.error(f"Tick processing error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    # ──────────────────────────────────────────────
    # Core tick — mirrors Pine Script calc_on_every_tick
    # ──────────────────────────────────────────────
    async def _on_tick(self, current_price: float) -> None:
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

        # Directional profit distances — matches Pine Script
        if is_long:
            peak_profit_dist = max(0.0, state.peak_price - entry_price)
            current_profit_dist = current_price - entry_price
        else:
            peak_profit_dist = max(0.0, entry_price - state.peak_price)
            current_profit_dist = entry_price - current_price

        # 1. Target Profit
        tp_hit = (
            (is_long and current_price >= risk.tp)
            or (not is_long and current_price <= risk.tp)
        )
        if tp_hit:
            await self._execute_exit(current_price, "Target Profit")
            return

        # 2. Max SL (blocked on entry bar — FIX-007)
        is_entry_bar = self._is_same_bar(current_time_ms)
        if not is_entry_bar and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # 3. Breakeven
        if not state.be_done and should_trigger_be(current_profit_dist, atr):
            state.be_done = True
            if self._sl_improves(entry_price, state.current_sl, is_long):
                old_sl = state.current_sl
                state.current_sl = entry_price
                logger.info(f"Breakeven | SL {old_sl:.2f} → {entry_price:.2f}")
                await self.telegram.notify_breakeven(entry_price)

        # 4. Trail Stage upgrade — FIX-STAGE-001
        # Pine: calc_on_every_tick=true, so 'close' = current tick price.
        # profitDist = close - entryPrice fires on EVERY TICK, not just bar close.
        # Both Lite model and re-reading Pine confirmed: use current_profit_dist.
        new_stage = calc_trail_stage(current_profit_dist, atr)
        if new_stage > state.stage:
            old_stage = state.stage
            state.stage = new_stage
            pts, _ = get_trail_params(state.stage, atr)
            logger.info(
                f"TRAIL stage {old_stage} → {state.stage} | "
                f"price={current_price:.2f} profit={current_profit_dist:.2f} pts={pts:.2f}"
            )
            await self.telegram.notify_trail_stage(
                old_stage, state.stage, current_price, state.current_sl
            )

        # 5. Trailing SL calculation (Pine: peak - trail_points, active from off threshold)
        trail_param_stage = state.stage if state.stage > 0 else 1
        pts, off = get_trail_params(trail_param_stage, atr)
        trail_active = peak_profit_dist >= off

        if trail_active:
            candidate_sl = (
                state.peak_price - pts if is_long else state.peak_price + pts
            )
            if self._sl_improves(candidate_sl, state.current_sl, is_long):
                old_sl = state.current_sl
                state.current_sl = candidate_sl
                logger.info(
                    f"TRAIL SL update | stage={max(state.stage, 1)} "
                    f"peak={state.peak_price:.2f} pts={pts:.2f} "
                    f"SL {old_sl:.2f} → {candidate_sl:.2f}"
                )

        # 6. Unified stop check: initial / breakeven / trail — matches Pine Script
        if state.current_sl > 0:
            sl_hit = (
                (is_long and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER)
                or (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_hit:
                trail_has_improved = (
                    (is_long and state.current_sl > risk.sl)
                    or (not is_long and state.current_sl < risk.sl)
                )
                if trail_has_improved:
                    reason = f"Trail S{max(state.stage, 1)}"
                elif state.be_done and abs(state.current_sl - entry_price) <= max(1e-9, atr * 1e-6):
                    reason = "Breakeven SL"
                else:
                    reason = "Initial SL"
                await self._execute_exit(current_price, reason)
                return

    # ──────────────────────────────────────────────
    # FIX-TV-001: Immediate exit — no bar-boundary wait
    # ──────────────────────────────────────────────
    async def _execute_exit(self, price: float, reason: str) -> None:
        # FIX-TV-002: prevent double-exit
        if self._exit_triggered:
            return
        self._exit_triggered = True
        self._running = False

        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(
                exit_order.get("average")
                or exit_order.get("price")
                or price
            )
        except Exception as e:
            logger.error(f"Exit order failed: {e}")
            fill_price = price

        real_pl = calc_real_pl(
            self.risk.entry_price, fill_price, self.risk.is_long, ALERT_QTY
        )

        # FIX-TV-003: real wall-clock time in log
        wall_time = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
        logger.info(
            f"Position closed | exit={fill_price:.2f} reason={reason} "
            f"pl={real_pl:.4f} time={wall_time}"
        )

        await self.telegram.notify_exit(
            reason, self.risk.entry_price, fill_price, real_pl
        )

        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)

    @staticmethod
    def _sl_improves(new_sl: float, current_sl: float, is_long: bool) -> bool:
        if current_sl == 0.0:
            return True
        return new_sl > current_sl if is_long else new_sl < current_sl
