"""
monitor/trail_loop.py - Shiva Sniper v6.5
Pine-Exact Trail Version

FIX-TRAIL-1 | trail_offset was discarded (underscore _) — now used correctly.
  Pine's strategy.exit(trail_offset=X) means: do NOT place a trailing SL until
  price has moved at least X points from entry.  The bot ignored this — it placed
  a trail SL the moment stage 1 activated (0.55 ATR), which could put the trail SL
  *below* the initial bracket SL on small moves and caused premature exits.
  Fix: candidate_sl is only applied when profit_dist >= trail_off for that stage.

FIX-TRAIL-2 | _sl_improved() now takes is_long explicitly — no ambiguity.
  Before: direction was inferred from current_sl sign (wrong after BE).
  Fix: is_long passed from risk, direction logic is explicit.

FIX-TRAIL-3 | BE and trail no longer race.
  BE sets current_sl = entry_price only if it is strictly better than current_sl.
  Trail then continues to ratchet upward from there (it won't go below entry after BE).

FIX-TRAIL-4 | Stage log now shows trail_pts and trail_off for easy debugging.
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
    TRAIL_SL_PRE_FIRE_BUFFER,
)

logger = logging.getLogger(__name__)

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
            "urls": {"api": {"public": _base_url, "private": _base_url}},
        }
        self._exchange = ccxt.delta(params)
        logger.info(f"Trail ticker polling every {FIXED_TRAIL_LOOP_SEC}s for {SYMBOL}")

        try:
            while self._running:
                await asyncio.sleep(FIXED_TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    # Use 'last' price to match TradingView charts
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

    async def _on_tick(self, current_price: float) -> None:
        if not self._running or self.risk is None or self.state is None:
            return

        risk        = self.risk
        state       = self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # ── 1. Update peak price (running high for long, running low for short) ──
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # ── 2. Profit distance from entry ─────────────────────────────────────
        # FIX-TRAIL-PARITY: two distances serve different purposes:
        #   peak_profit_dist  → for STAGE ADVANCEMENT (peak-based, never regresses;
        #                        matches Pine's trailStage which only moves up)
        #   current_profit_dist → for trail_offset GATE and BE trigger
        #                         (current tick distance, matches Pine trail_offset)
        peak_profit_dist    = abs(state.peak_price - entry_price)
        current_profit_dist = abs(current_price    - entry_price)

        # ── 3. TP check ───────────────────────────────────────────────────────
        tp_hit = (is_long     and current_price >= risk.tp) or \
                 (not is_long and current_price <= risk.tp)
        if tp_hit:
            await self._execute_exit(current_price, "Target Profit")
            return

        # ── 4. SL / Trail SL breach check ─────────────────────────────────────
        # PRE-FIRE: trigger TRAIL_SL_PRE_FIRE_BUFFER pts BEFORE the SL is
        # actually breached. The market exit order then fills near the intended
        # SL price instead of after a gap. On BTC 30m, prices can gap 50-100 pts
        # in one 0.5s tick — firing 8 pts early closes that slippage.
        if state.current_sl > 0:
            sl_breached = (
                (is_long     and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_breached:
                reason = "Initial SL" if (state.stage == 0 and not state.be_done) \
                         else f"Trail S{state.stage}"
                await self._execute_exit(current_price, reason)
                return

        # ── 5. Max SL (emergency hard cap) ───────────────────────────────────
        if not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
            state.max_sl_fired = True
            await self._execute_exit(current_price, "Max SL Hit")
            return

        # ── 6. Breakeven ──────────────────────────────────────────────────────
        if not state.be_done and should_trigger_be(current_profit_dist, atr):
            state.be_done = True
            # Only move SL to entry if that improves it (won't regress a trail SL)
            if self._sl_improved(entry_price, state.current_sl, is_long):
                state.current_sl = entry_price
                logger.info(f"BE locked | current_sl → entry={entry_price:.2f}")
            await self.telegram.notify_breakeven(entry_price)

        # ── 7. Trail stage advancement (peak-based — Pine parity) ─────────────
        # Pine: trailStage var only moves forward (never resets on pullback).
        # Using peak_profit_dist ensures that if price hit e.g. 2.1×ATR and
        # pulled back to 1.5×ATR, stage 2 stays active — same as Pine's var.
        new_stage = calc_trail_stage(peak_profit_dist, atr)
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            trail_pts, trail_off = get_trail_params(state.stage, atr)
            logger.info(
                f"TRAIL stage {old_stage} → {state.stage} | "
                f"trail_pts={trail_pts:.2f} trail_off={trail_off:.2f}"
            )
            await self.telegram.notify_trail_stage(
                old_stage, state.stage, current_price, state.current_sl
            )

        # ── 8. Trail SL placement (FIX-TRAIL-1: offset gate) ─────────────────
        # Pine: strategy.exit(trail_points=X, trail_offset=Y)
        #   → trail SL is only PLACED once price moves Y pts from entry.
        #   → trail SL then sits X pts behind the running peak.
        if state.stage > 0:
            trail_pts, trail_off = get_trail_params(state.stage, atr)

            # Gate: trail SL only activates after current_profit_dist >= trail_off
            # (matching Pine's trail_offset semantics: activation from fill price)
            if current_profit_dist >= trail_off:
                candidate_sl = (state.peak_price - trail_pts) if is_long \
                               else (state.peak_price + trail_pts)

                if self._sl_improved(candidate_sl, state.current_sl, is_long):
                    state.current_sl = candidate_sl
                    logger.info(
                        f"TRAIL SL update | stage={state.stage} "
                        f"peak={state.peak_price:.2f} "
                        f"pts={trail_pts:.2f} "
                        f"new_sl={candidate_sl:.2f}"
                    )
                    self.journal.update_open_trade(
                        trail_stage=state.stage,
                        current_sl=state.current_sl,
                        peak_price=state.peak_price,
                    )

    async def _execute_exit(self, price: float, reason: str):
        self._running = False
        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            exit_price = float(
                exit_order.get("average") or exit_order.get("price") or price
            )
        except Exception as e:
            logger.error(f"Exit failed: {e}")
            exit_price = price

        real_pl = calc_real_pl(
            self.risk.entry_price, exit_price, self.risk.is_long, ALERT_QTY
        )
        await self.telegram.notify_exit(reason, self.risk.entry_price, exit_price, real_pl)
        if self.on_trail_exit:
            await self.on_trail_exit(exit_price, reason)

    @staticmethod
    def _sl_improved(new_sl: float, current_sl: float, is_long: bool) -> bool:
        """Return True if new_sl is strictly better (tighter) than current_sl.
        For longs: higher SL is better (moves toward entry/profit).
        For shorts: lower SL is better.
        If current_sl is 0.0 (not yet set), always accept.
        """
        if current_sl == 0.0:
            return True
        return new_sl > current_sl if is_long else new_sl < current_sl
