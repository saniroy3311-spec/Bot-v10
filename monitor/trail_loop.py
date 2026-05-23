"""
monitor/trail_loop.py — Shiva Sniper v11 — PINE-EXACT-TRAIL
════════════════════════════════════════════════════════════════════════════

COMPLETE REWRITE — v11 Pine-Exact Trail Engine
════════════════════════════════════════════════════════════════════════════

ROOT CAUSE OF ALL PREVIOUS DIVERGENCE
──────────────────────────────────────────────────────────────────────────
Previous versions tried to APPROXIMATE Pine's trail by computing a trail SL
from peak_price and an offset. This never matched because:

  1. MIN_ARM_ATR_MULT (4.0×) — artificial floor not in Pine
  2. LOOSE_OFFSET_ATR_MULT (6.0×) — artificial wide offset not in Pine
  3. Peak tracking from WS ticks ≠ Pine's internal best_price tracking
  4. Trail SL computation intrabar drifted with every tick

HOW PINE'S trail_points / trail_offset ACTUALLY WORKS
──────────────────────────────────────────────────────────────────────────
Pine's strategy.exit(trail_points=P, trail_offset=O) internally does:

  SHORT TRADE:
    Step 1 — ACTIVATION:
      Trail is NOT active until price drops P points below entry.
      activation_price = entryPrice - P        (for short, P = profit direction)
      trail armed when price <= activation_price

    Step 2 — BEST PRICE tracking (once armed):
      best_price = lowest price seen since trail armed
      best_price updates on every tick: best_price = min(best_price, current_price)

    Step 3 — TRAIL SL:
      trail_sl = best_price + O
      Exit fires when current_price >= trail_sl

  LONG TRADE:
    activation_price = entryPrice + P
    best_price = highest price seen since trail armed
    trail_sl = best_price - O
    Exit fires when current_price <= trail_sl

  KEY INSIGHT: Pine tracks best_price from the moment trail ARMS,
  not from entry. The bot must do exactly this — no peak_profit_dist
  approximation, no artificial floors, no LOOSE_OFFSET.

STAGE UPGRADES
──────────────────────────────────────────────────────────────────────────
Pine upgrades the trail stage when profitDist >= atr * triggerMult.
When stage upgrades, new P and O values are used immediately.
The trail_sl is recomputed from best_price with the new offset.
best_price does NOT reset on stage upgrade — it is the running best
since the trail first armed.

PINE MINTICK
──────────────────────────────────────────────────────────────────────────
Pine passes raw ATR multiples to trail_points and trail_offset.
These are in TICK units, not price units.
  price_pts = atr * mult * PINE_MINTICK
  e.g. ATR=224, mult=0.70, mintick=0.1 → 224 * 0.70 * 0.1 = 15.68 pts

INITIAL SL (Dynamic per bar)
──────────────────────────────────────────────────────────────────────────
Pine calls strategy.exit(stop=shortSL) EVERY bar outside any if block.
  shortSL = entryPrice + min(atr * atrMultActive, maxSLPoints)
  atr changes each bar → SL shifts bidirectionally.
Bot replicates this in on_bar_close() when trail not yet armed.

BREAKEVEN
──────────────────────────────────────────────────────────────────────────
Pine: if profitDist > atr * beMult → override stop = entryPrice
Once BE fires, trail continues from entryPrice as new worst-allowed SL.
BE does NOT disable trailing — it just sets a floor on the SL.

════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from config import (
    TRAIL_STAGES, BE_MULT, MAX_SL_MULT, MAX_SL_POINTS,
    TRAIL_LOOP_SEC, TRAIL_SL_PRE_FIRE_BUFFER,
    CANDLE_TIMEFRAME, TIME_EXIT_MINUTES, PINE_MINTICK,
    TREND_ATR_MULT, RANGE_ATR_MULT,
)
from risk.calculator import RiskLevels, TrailState

logger = logging.getLogger("trail_loop")


# ─── Timeframe → milliseconds ──────────────────────────────────────────────────

def _tf_to_ms(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    return 1_800_000

BAR_PERIOD_MS = _tf_to_ms(CANDLE_TIMEFRAME)


# ─── Pine trail engine helpers ─────────────────────────────────────────────────

def _trail_pts(stage: int, atr: float) -> float:
    """
    Activation distance in price points.
    Pine: trail_points = atr * pts_mult  (tick units → price via PINE_MINTICK)
    """
    idx = max(stage - 1, 0)
    _, pts_mult, _ = TRAIL_STAGES[idx]
    return atr * pts_mult * PINE_MINTICK


def _trail_off(stage: int, atr: float) -> float:
    """
    Offset distance in price points.
    Pine: trail_offset = atr * off_mult  (tick units → price via PINE_MINTICK)
    """
    idx = max(stage - 1, 0)
    _, _, off_mult = TRAIL_STAGES[idx]
    return atr * off_mult * PINE_MINTICK


def _activation_price(entry: float, stage: int, atr: float, is_long: bool) -> float:
    """
    Price at which trail arms.
    Long:  entry + trail_pts  (price must rise this far)
    Short: entry - trail_pts  (price must fall this far)
    """
    pts = _trail_pts(stage, atr)
    return (entry + pts) if is_long else (entry - pts)


def _trail_sl_from_best(best_price: float, stage: int, atr: float, is_long: bool) -> float:
    """
    Trail SL level given the current best_price.
    Long:  best_price - offset  (SL trails below the peak)
    Short: best_price + offset  (SL trails above the trough)
    """
    off = _trail_off(stage, atr)
    return (best_price - off) if is_long else (best_price + off)


def _upgrade_stage(current_stage: int, profit_dist: float, atr: float) -> int:
    """
    Returns the highest trail stage unlocked by profit_dist.
    Stages only upgrade, never downgrade — matches Pine's `var trailStage`.
    Pine: profitDist >= atr * triggerMult  (raw pts, no PINE_MINTICK)
    """
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if profit_dist >= atr * trigger_mult:
            candidate = i + 1
            if candidate > new_stage:
                new_stage = candidate
            break
    return new_stage


# ─── Extended TrailState fields ────────────────────────────────────────────────
# We add runtime attributes to TrailState dynamically:
#   trail_armed     : bool  — True once activation_price crossed
#   best_price      : float — lowest (short) or highest (long) since trail armed
# These are not in the dataclass to avoid changing risk/calculator.py


# ─── TrailMonitor ──────────────────────────────────────────────────────────────

class TrailMonitor:
    """
    Tick-resolution trailing stop monitor — exact Pine Script parity.

    Pine's trail_points / trail_offset engine replicated exactly:
      • Trail arms when price crosses activation_price (entry ± trail_pts)
      • best_price tracks the running extreme since arming
      • trail_sl = best_price ± trail_offset
      • Stage upgrades ratchet up using profit_dist >= atr * trigger
      • On stage upgrade, trail_sl recomputes from existing best_price
      • Initial SL updates every bar with live ATR (matches Pine's strategy.exit recalc)
      • Breakeven sets SL floor at entry_price, trail continues above it

    on_bar_close()   → ATR update + stage upgrade + BE + initial SL update
    on_price_tick()  → primary intrabar exit (WS feed)
    _tick_loop()     → 5-second REST safety-net backup
    push_ws_candle() → intrabar peak update + immediate exit eval
    """

    def __init__(self, order_mgr, telegram, journal) -> None:
        self._order_mgr = order_mgr
        self._telegram  = telegram
        self._journal   = journal

        self._running          : bool = False
        self._risk             : Optional[RiskLevels] = None
        self._state            : Optional[TrailState] = None
        self._on_exit_cb       : Optional[Callable]   = None
        self._entry_bar_ms     : int  = 0
        self._entry_bar_end_ms : int  = 0
        self._task             : Optional[asyncio.Task] = None
        self._exit_fired       : bool = False

        self._current_atr      : float = 0.0  # updated only at bar close

        self._entry_wall_ms    : int   = 0

        # Source offset (Binance→Delta price compensation)
        self._source_offset    : Optional[float] = None
        self._first_tick_ts_ms : int  = 0

        # Offset recalibration
        self._last_recal_ms     : int  = 0
        self._recal_interval_ms : int  = 30_000
        self._recal_in_progress : bool = False

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels      : RiskLevels,
        trail_state      : TrailState,
        entry_bar_time_ms: int,
        on_trail_exit    : Callable,
        entry_wall_ms    : Optional[int] = None,
    ) -> None:
        self._risk         = risk_levels
        self._state        = trail_state
        self._on_exit_cb   = on_trail_exit
        self._entry_bar_ms = entry_bar_time_ms
        self._exit_fired   = False
        self._running      = True
        self._current_atr  = risk_levels.atr

        # Pine trail runtime state — attached dynamically to avoid changing dataclass
        trail_state.trail_armed = False
        trail_state.best_price  = 0.0

        self._entry_wall_ms = entry_wall_ms if entry_wall_ms is not None else int(time.time() * 1000)
        elapsed_already = (int(time.time() * 1000) - self._entry_wall_ms) // 1000
        if TIME_EXIT_MINUTES > 0 and elapsed_already > 0:
            logger.info(
                f"[TRAIL] Time exit: trade already {elapsed_already}s old at start "
                f"(limit={TIME_EXIT_MINUTES * 60}s)"
            )

        self._source_offset    = None
        self._first_tick_ts_ms = 0

        self._entry_bar_end_ms = (
            (entry_bar_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        ) + BAR_PERIOD_MS

        self._task = asyncio.get_running_loop().create_task(self._tick_loop())
        logger.info(
            f"[TRAIL] Started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"entry_atr={risk_levels.atr:.2f} is_long={risk_levels.is_long} | "
            f"trail_pts={_trail_pts(1, risk_levels.atr):.2f} "
            f"trail_off={_trail_off(1, risk_levels.atr):.2f}"
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("TrailMonitor stopped.")

    # ── Bar-close update ──────────────────────────────────────────────────────

    def on_bar_close(
        self,
        bar_close  : float,
        bar_high   : float,
        bar_low    : float,
        bar_open   : float,
        current_atr: float,
    ) -> None:
        """
        Called at every confirmed bar close.

        1. Update live ATR
        2. Update initial SL from live ATR (Pine recalcs stop= every bar)
        3. Stage upgrade from bar-close profit
        4. Breakeven check from bar-close profit
        5. Update best_price from bar extreme (if trail already armed)
        6. Check trail arm from bar extreme (if not yet armed)
        7. Recompute trail_sl from best_price
        8. Same-bar exit check (TP / SL)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price

        # Source offset translation
        if self._source_offset is not None:
            bar_close = bar_close - self._source_offset
            bar_high  = bar_high  - self._source_offset
            bar_low   = bar_low   - self._source_offset
            if bar_open > 0.0:
                bar_open = bar_open - self._source_offset

        # ── 1. Update live ATR ───────────────────────────────────────────────
        self._current_atr = current_atr

        # ── 2. Initial SL update (Pine recalcs stop= every bar) ─────────────
        # Only when trail not yet armed — once trail arms, current_sl is the trail SL
        if not getattr(state, 'trail_armed', False) and not state.be_done:
            _atr_mult  = TREND_ATR_MULT if risk.is_trend else RANGE_ATR_MULT
            _stop_dist = min(current_atr * _atr_mult, MAX_SL_POINTS)
            _new_sl    = (entry_price - _stop_dist) if is_long else (entry_price + _stop_dist)
            if abs(_new_sl - state.current_sl) > 0.01:
                logger.info(
                    f"[TRAIL] Initial SL update: {state.current_sl:.2f} → {_new_sl:.2f} "
                    f"(atr={current_atr:.2f} stop_dist={_stop_dist:.2f})"
                )
            state.current_sl = _new_sl

        # ── 3. Stage upgrade from bar-close profit ───────────────────────────
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, current_atr)
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} → {new_stage} at bar close | "
                f"profit={close_profit:.2f} atr={current_atr:.2f}"
            )
            state.stage = new_stage
            # Recompute trail_sl from existing best_price with new stage offset
            if getattr(state, 'trail_armed', False):
                new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, current_atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="stage_upgrade_bar")

        # ── 4. Breakeven check ───────────────────────────────────────────────
        if not state.be_done and close_profit > current_atr * BE_MULT:
            self._activate_be(state, risk, is_long, current_atr, source="bar_close")

        # ── 5 & 6. Bar extreme: update best_price or check trail arm ─────────
        bar_extreme = bar_high if is_long else bar_low
        bar_profit  = (bar_extreme - entry_price) if is_long else (entry_price - bar_extreme)

        # Snapshot SL before trail update (for same-bar exit check)
        pre_trail_sl = state.current_sl

        if getattr(state, 'trail_armed', False):
            # Update best_price from bar extreme
            self._update_best_price(state, bar_extreme, is_long)
            # Recompute trail SL
            new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, current_atr, is_long)
            self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="bar_close")
        else:
            # Check if bar extreme crossed activation price
            act_price = _activation_price(entry_price, max(state.stage, 1), current_atr, is_long)
            armed = (bar_extreme >= act_price) if is_long else (bar_extreme <= act_price)
            if armed:
                state.trail_armed = True
                state.best_price  = bar_extreme
                new_trail_sl = _trail_sl_from_best(state.best_price, max(state.stage, 1), current_atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="bar_close_arm")
                logger.info(
                    f"[TRAIL] Trail ARMED at bar close | best={bar_extreme:.2f} "
                    f"trail_sl={state.current_sl:.2f} act_price={act_price:.2f}"
                )

        # ── 7. Same-bar exit check ────────────────────────────────────────────
        # Use pre_trail_sl — the SL that was active at bar open
        tp_hit = (bar_high >= risk.tp)      if is_long else (bar_low  <= risk.tp)
        sl_hit = (bar_low  <= pre_trail_sl) if is_long else (bar_high >= pre_trail_sl)

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                ref     = bar_open if bar_open > 0.0 else bar_close
                use_tp  = abs(ref - risk.tp) <= abs(ref - pre_trail_sl)
                exit_px = risk.tp       if use_tp else pre_trail_sl
                reason  = "TP (bar)"   if use_tp else "SL (bar)"
            elif tp_hit:
                exit_px = risk.tp
                reason  = "TP (bar)"
            else:
                exit_px = pre_trail_sl
                reason  = "Trail SL (bar)" if getattr(state, 'trail_armed', False) else "Initial SL (bar)"

            logger.info(f"[TRAIL] Same-bar exit: {reason} @ {exit_px:.2f}")
            asyncio.get_running_loop().create_task(
                self._fire_exit(exit_px, reason, source="bar_close")
            )

    # ── WS price push — primary exit detection ─────────────────────────────────

    async def on_price_tick(self, price: float, source: str = "binance") -> None:
        """Primary intrabar exit path — called from WS feed on every tick."""
        # SAFETY GUARD: Ignore raw Binance calculations to prevent mathematical drift
        if source == "binance":
            return

        if not self._running or self._exit_fired or price <= 0:
            return

        if source == "binance" and self._risk is not None:
            if self._source_offset is None:
                raw_offset = price - self._risk.entry_price
                if abs(raw_offset) > 500.0:
                    logger.warning(
                        f"[TRAIL] Source offset rejected (|{raw_offset:+.2f}| > 500): "
                        f"binance={price:.2f} delta_fill={self._risk.entry_price:.2f}"
                    )
                    return
                self._source_offset    = raw_offset
                self._first_tick_ts_ms = int(time.time() * 1000)
                logger.info(
                    f"[TRAIL] Source offset: binance={price:.2f} "
                    f"delta={self._risk.entry_price:.2f} offset={self._source_offset:+.2f}"
                )
            price = price - self._source_offset

            now_ms = int(time.time() * 1000)
            if (
                not self._recal_in_progress
                and now_ms - self._last_recal_ms >= self._recal_interval_ms
            ):
                self._recal_in_progress = True
                asyncio.get_running_loop().create_task(
                    self._recalibrate_offset(price + self._source_offset)
                )

        await self._evaluate_tick(price)

    async def _recalibrate_offset(self, binance_price_raw: float) -> None:
        try:
            delta_mark = await self._get_mark_price()
            if delta_mark and delta_mark > 0 and self._source_offset is not None:
                new_offset = binance_price_raw - delta_mark
                if abs(new_offset - self._source_offset) <= 50.0:
                    old = self._source_offset
                    self._source_offset = new_offset
                    logger.info(
                        f"[TRAIL] Offset recalibrated: {old:+.2f} → {new_offset:+.2f} "
                        f"(binance={binance_price_raw:.2f} delta={delta_mark:.2f})"
                    )
                else:
                    logger.warning(
                        f"[TRAIL] Offset recal rejected: "
                        f"old={self._source_offset:+.2f} new={new_offset:+.2f}"
                    )
        except Exception as e:
            logger.warning(f"[TRAIL] Offset recal failed: {e}")
        finally:
            self._last_recal_ms     = int(time.time() * 1000)
            self._recal_in_progress = False

    async def _evaluate_tick_pair(self, tp_side: float, sl_side: float) -> None:
        await self._evaluate_tick(tp_side)
        if not self._exit_fired:
            await self._evaluate_tick(sl_side)

    # ── Safety-net REST poll ───────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while self._running and not self._exit_fired:
            try:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                if not self._running or self._exit_fired:
                    break
                price = await self._get_mark_price()
                if price is None or price <= 0:
                    continue
                await self._evaluate_tick(price)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRAIL] Tick loop error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    # ── Core tick evaluator — Pine trail engine ────────────────────────────────

    async def _evaluate_tick(self, price: float) -> None:
        """
        Pine trail_points / trail_offset engine — exact replication.

        For every price tick:
          1. Compute profit_dist from entry
          2. Stage upgrade check (ratchet only up)
          3. Breakeven check
          4. TP hit check
          5. Initial SL check (if trail not armed)
          6. Trail arm check (if not armed): price crosses activation_price?
          7. best_price update (if armed)
          8. trail_sl recompute from best_price
          9. Trail SL hit check
          10. Max SL check
          11. Time exit check
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = self._current_atr

        # Profit from entry to current price
        profit_dist = (price - entry_price) if is_long else (entry_price - price)

        # ── 1. Stage upgrade ──────────────────────────────────────────────────
        # PINE PARITY FIX: Stage upgrades happen ONLY on bar close (not on ticks).
        # Pine updates trailStage only when the script re-evaluates at bar close.
        # Tick-level upgrades cause the bot to tighten the trail mid-bar before
        # Pine would, creating premature exits on volatile bars.
        #
        # The bar-close stage upgrade is handled in on_bar_close() (line 333).
        # This tick-level upgrade block is now disabled for Pine parity.
        #
        # ORIGINAL CODE (now commented out):
        # new_stage = _upgrade_stage(state.stage, profit_dist, atr)
        # if new_stage > state.stage:
        #     logger.info(
        #         f"[TRAIL] Stage {state.stage} → {new_stage} (intrabar tick) | "
        #         f"profit={profit_dist:.2f} atr={atr:.2f}"
        #     )
        #     state.stage = new_stage
        #     # Recompute trail SL immediately with new stage offset
        #     if getattr(state, 'trail_armed', False):
        #         new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, atr, is_long)
        #         self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="stage_upgrade_tick")

        # ── 2. Breakeven check ────────────────────────────────────────────────
        if not state.be_done and profit_dist > atr * BE_MULT:
            self._activate_be(state, risk, is_long, atr, source="tick")

        # ── 3. TP hit ─────────────────────────────────────────────────────────
        if is_long and price >= risk.tp:
            await self._fire_exit(risk.tp, "TP", source="tick")
            return
        if not is_long and price <= risk.tp:
            await self._fire_exit(risk.tp, "TP", source="tick")
            return

        # ── 4 & 5. Trail arm or initial SL ───────────────────────────────────
        if not getattr(state, 'trail_armed', False):
            # Check activation: has price moved trail_pts in profit direction?
            act_price = _activation_price(entry_price, max(state.stage, 1), atr, is_long)
            armed = (price >= act_price) if is_long else (price <= act_price)

            if armed:
                # Trail just armed this tick
                state.trail_armed = True
                state.best_price  = price
                new_trail_sl = _trail_sl_from_best(price, max(state.stage, 1), atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="arm_tick")
                logger.info(
                    f"[TRAIL] Trail ARMED | price={price:.2f} "
                    f"act_price={act_price:.2f} "
                    f"trail_sl={state.current_sl:.2f} "
                    f"trail_pts={_trail_pts(max(state.stage,1), atr):.2f} "
                    f"trail_off={_trail_off(max(state.stage,1), atr):.2f}"
                )
            else:
                # Trail not armed yet — check initial / BE SL only
                sl_hit = (
                    (price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) if is_long
                    else (price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
                )
                if sl_hit:
                    reason = "Breakeven SL" if state.be_done else "Initial SL"
                    await self._fire_exit(price, reason, source="tick")
                    return

                # Max SL check (entry bar exempt)
                if not state.max_sl_fired:
                    entry_bar_over = (time.time() * 1000) >= self._entry_bar_end_ms
                    max_thresh     = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
                    if entry_bar_over:
                        if is_long  and price <= entry_price - max_thresh:
                            state.max_sl_fired = True
                            await self._fire_exit(price, "Max SL", source="tick")
                            return
                        if not is_long and price >= entry_price + max_thresh:
                            state.max_sl_fired = True
                            await self._fire_exit(price, "Max SL", source="tick")
                            return

                # Time exit check
                if TIME_EXIT_MINUTES > 0 and self._entry_wall_ms > 0:
                    elapsed_ms = int(time.time() * 1000) - self._entry_wall_ms
                    if elapsed_ms >= TIME_EXIT_MINUTES * 60_000:
                        await self._fire_exit(price, f"Time exit ({TIME_EXIT_MINUTES}m)", source="tick")
                        return
                return

        # ── 6. Trail is armed — update best_price ────────────────────────────
        self._update_best_price(state, price, is_long)

        # ── 7. Recompute trail SL from best_price ────────────────────────────
        new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, atr, is_long)
        self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="tick")

        # ── 8. Trail SL hit check ─────────────────────────────────────────────
        sl_hit = (
            (price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) if is_long
            else (price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
        )
        if sl_hit:
            trail_improved = (
                (state.current_sl > risk.sl) if is_long
                else (state.current_sl < risk.sl)
            )
            be_at_entry = state.be_done and abs(state.current_sl - entry_price) < 1e-6
            if be_at_entry:
                reason = "Breakeven SL"
            elif trail_improved:
                reason = f"Trail SL (stage {state.stage})"
            else:
                reason = "Initial SL"
            await self._fire_exit(price, reason, source="tick")
            return

        # ── 9. Max SL (entry bar exempt) ─────────────────────────────────────
        if not state.max_sl_fired:
            entry_bar_over = (time.time() * 1000) >= self._entry_bar_end_ms
            max_thresh     = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
            if entry_bar_over:
                if is_long  and price <= entry_price - max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return
                if not is_long and price >= entry_price + max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return

        # ── 10. Time exit ─────────────────────────────────────────────────────
        if TIME_EXIT_MINUTES > 0 and self._entry_wall_ms > 0:
            elapsed_ms = int(time.time() * 1000) - self._entry_wall_ms
            if elapsed_ms >= TIME_EXIT_MINUTES * 60_000:
                await self._fire_exit(price, f"Time exit ({TIME_EXIT_MINUTES}m)", source="tick")

    # ── Trail helpers ──────────────────────────────────────────────────────────

    def _update_best_price(self, state: TrailState, price: float, is_long: bool) -> None:
        """Update best_price — highest for long, lowest for short."""
        if is_long:
            if price > state.best_price:
                state.best_price = price
        else:
            if state.best_price == 0.0 or price < state.best_price:
                state.best_price = price

    def _apply_trail_sl(
        self,
        state   : TrailState,
        risk    : RiskLevels,
        new_sl  : float,
        is_long : bool,
        source  : str = "",
    ) -> None:
        """
        Apply new_sl to state.current_sl only if it improves (moves toward profit).
        Long:  SL can only move up   (higher = better for long)
        Short: SL can only move down (lower  = better for short)
        Also enforces BE floor if breakeven is active.
        """
        # Enforce BE floor: SL cannot go worse than entry_price once BE fired
        if state.be_done:
            if is_long:
                new_sl = max(new_sl, risk.entry_price)
            else:
                new_sl = min(new_sl, risk.entry_price)

        # Only move SL in the favourable direction
        if is_long and new_sl > state.current_sl:
            logger.info(
                f"[TRAIL] Trail SL: {state.current_sl:.2f} → {new_sl:.2f} "
                f"(stage {state.stage} best={state.best_price:.2f} src={source})"
            )
            state.current_sl = new_sl
        elif not is_long and new_sl < state.current_sl:
            logger.info(
                f"[TRAIL] Trail SL: {state.current_sl:.2f} → {new_sl:.2f} "
                f"(stage {state.stage} best={state.best_price:.2f} src={source})"
            )
            state.current_sl = new_sl

    def _activate_be(
        self,
        state   : TrailState,
        risk    : RiskLevels,
        is_long : bool,
        atr     : float,
        source  : str = "",
    ) -> None:
        """Activate breakeven — set SL floor at entry_price."""
        be_sl = risk.entry_price
        improved = (be_sl > state.current_sl) if is_long else (be_sl < state.current_sl)
        if improved:
            state.current_sl = be_sl
            state.be_done    = True
            logger.info(
                f"[TRAIL] Breakeven activated ({source}): SL → {be_sl:.2f} "
                f"(atr={atr:.2f})"
            )
        else:
            # Trail already past entry — just mark BE done, don't pull SL back
            state.be_done = True
            logger.info(
                f"[TRAIL] Breakeven noted ({source}) but trail SL {state.current_sl:.2f} "
                f"already past entry {be_sl:.2f} — no SL change"
            )

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """Fire exit once. Idempotent."""
        if self._exit_fired:
            return
        self._exit_fired = True

        logger.info(
            f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f} "
            f"source={source} atr={self._current_atr:.2f}"
        )

        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed: {e}")

        is_long = self._risk.is_long if self._risk else True

        MAX_ATTEMPTS = 3
        success = False
        actual_fill_price: Optional[float] = None
        last_err: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await self._order_mgr.close_position(is_long=is_long, reason=reason)
                success = True
                if isinstance(result, dict):
                    if result.get("info") == "already_closed":
                        logger.info("[TRAIL] Position already closed on exchange.")
                    else:
                        fill = result.get("average") or result.get("price")
                        if fill and float(fill) > 0:
                            actual_fill_price = float(fill)
                        logger.info(
                            f"[TRAIL] Exit order placed (attempt {attempt}) "
                            f"fill={actual_fill_price}"
                        )
                break
            except Exception as e:
                last_err = e
                logger.warning(f"[TRAIL] close_position attempt {attempt}/{MAX_ATTEMPTS}: {e}")
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)

        if not success:
            logger.error(
                f"[TRAIL] close_position FAILED after {MAX_ATTEMPTS} attempts "
                f"(last: {last_err}). ⚠️ MANUAL CHECK REQUIRED."
            )

        reported_price = actual_fill_price if actual_fill_price is not None else exit_price
        if actual_fill_price is not None and abs(actual_fill_price - exit_price) > 1.0:
            logger.info(
                f"[TRAIL] Fill correction: signal={exit_price:.2f} "
                f"actual={actual_fill_price:.2f} diff={actual_fill_price - exit_price:+.2f}"
            )

        self._running = False
        if self._on_exit_cb is not None:
            try:
                # position_already_closed=True: cancel_all_orders() + close_position()
                # ran above, so Delta is confirmed flat before the callback fires.
                await self._on_exit_cb(
                    reported_price,
                    reason,
                    source,
                    True,   # position_already_closed
                )
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch ───────────────────────────────────────────────────

    async def _get_mark_price(self) -> Optional[float]:
        try:
            ticker = await self._order_mgr.fetch_ticker()
            if ticker is None:
                return None
            mark = (
                ticker.get("markPrice")
                or (ticker.get("info") or {}).get("mark_price")
                or ticker.get("last")
                or 0.0
            )
            price = float(mark) if mark else 0.0
            return price if price > 0 else None
        except Exception as e:
            logger.warning(f"[TRAIL] _get_mark_price failed: {e}")
            return None

    # ── Feed integration ───────────────────────────────────────────────────────

    def _update_live_atr(self, high: float, low: float) -> None:
        """Disabled — ATR only updates at bar close."""
        return

    def push_ws_candle(self, high: float, low: float, source: str = "binance") -> None:
        """
        Called by ws_feed on every intrabar WS candle update.
        Evaluates both TP-side and SL-side prices immediately.
        """
        # SAFETY GUARD: Ignore raw Binance calculations to prevent mathematical drift
        if source == "binance":
            return

        if not self._running or self._exit_fired or self._state is None or self._risk is None:
            return

        is_long = self._risk.is_long

        if source == "binance":
            if self._source_offset is None:
                return
            high = high - self._source_offset
            low  = low  - self._source_offset

        try:
            loop    = asyncio.get_running_loop()
            tp_side = high if is_long else low
            sl_side = low  if is_long else high
            loop.create_task(self._evaluate_tick_pair(tp_side, sl_side))
        except RuntimeError:
            pass
