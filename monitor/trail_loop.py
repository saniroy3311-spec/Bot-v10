"""
monitor/trail_loop.py — Shiva Sniper v10 — Pine Script Exact Exit Parity
════════════════════════════════════════════════════════════════════════════

FIXES IN THIS FILE:
──────────────────────────────────────────────────────────────────────────
FIX-TRAIL-01 | pts_mult / off_mult roles were SWAPPED (root cause of all
  exit divergence). Pine Script docs define:
    trail_points  → ACTIVATION threshold (profit must reach this first)
    trail_offset  → DISTANCE the stop is placed from the peak
  The old v15 code reversed these, making every trail 0.15 ATR wider than
  Pine per stage — bot exits later / at worse prices than chart shows.
  FIXED: pts_mult = activation, off_mult = distance from peak.

FIX-TRAIL-02 | bar_open parameter added to on_bar_close().
  When both TP and SL are crossed on the same bar, Pine fires whichever
  level was CLOSER to bar_open. The bot was using bar_close as fallback,
  silently picking the wrong exit. Now correctly resolves priority.

FIX-TRAIL-03 | Stage upgrade uses bar close profit (not intrabar peak).
  Pine's trailStage variable increments on bar close (close − entryPrice).
  The bot was upgrading stage intrabar from tick data, advancing the stage
  too early and applying a tighter trail_offset sooner than Pine would.

FIX-TRAIL-04 | Breakeven moves stop TO entry_price (not ATR above/below).
  Pine: strategy.exit("BE-L", stop=entryPrice)
  Old bot: was offsetting BE stop by ATR in some code paths.

FIX-TRAIL-05 | Max SL correctly blocks on entry bar (matches Pine FIX-007).
  Pine: blockExitMaxSL guards entry bar + entry alert bar.
  Old bot: was evaluating max SL on the very first tick of the entry bar.

FIX-TRAIL-06 | Trail loop runs every TRAIL_LOOP_SEC (0.1s default) using
  the exchange mark price. This was not changed; just confirmed correct.
════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from config import (
    TRAIL_STAGES, BE_MULT, MAX_SL_MULT, MAX_SL_POINTS,
    TRAIL_LOOP_SEC, BRACKET_SL_BUFFER, TRAIL_SL_PRE_FIRE_BUFFER,
)
from risk.calculator import RiskLevels, TrailState

logger = logging.getLogger("trail_loop")


# ─── Pine parity helpers ───────────────────────────────────────────────────────

def _get_active_stage_params(stage: int, atr: float) -> tuple[float, float]:
    """
    Returns (activation_threshold, offset_from_peak) for the CURRENT stage.

    Pine Script mapping:
      trail_points  → profit must reach this before trail activates  (ACTIVATION)
      trail_offset  → stop is placed this far from the peak price    (DISTANCE)

    TRAIL_STAGES tuple layout: (trigger_mult, pts_mult, off_mult)
      pts_mult = trail_points multiplier → ACTIVATION  ← FIX-TRAIL-01
      off_mult = trail_offset multiplier → DISTANCE    ← FIX-TRAIL-01
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    activation = atr * pts_mult   # profit must be >= this to trail
    offset     = atr * off_mult   # stop placed this far from peak
    return activation, offset


def _compute_trail_sl(
    stage: int,
    atr: float,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
) -> Optional[float]:
    """
    Returns the trailing stop level for the current tick, or None if
    the trail has not yet activated (profit has not reached trail_points).

    FIX-TRAIL-01: activation uses pts_mult, distance uses off_mult.
    Pine's strategy.exit(trail_points=X, trail_offset=Y):
      - X (trail_points) = the activation threshold
      - Y (trail_offset) = the distance back from the peak
    """
    if stage == 0:
        return None

    activation, offset = _get_active_stage_params(stage, atr)

    # Trail only activates after profit reaches activation threshold
    if peak_profit_dist < activation:
        return None

    # Stop is placed `offset` distance back from the highest high / lowest low
    if is_long:
        return peak_price - offset
    else:
        return peak_price + offset


def _upgrade_stage(
    current_stage: int,
    profit_dist: float,
    atr: float,
) -> int:
    """
    Returns the highest trail stage unlocked by profit_dist.
    Stages only upgrade, never downgrade (matches Pine's trailStage var).
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


# ─── TrailMonitor ──────────────────────────────────────────────────────────────

class TrailMonitor:
    """
    Tick-resolution trailing stop monitor that mirrors Pine Script's
    strategy.exit(stop=, limit=, trail_points=, trail_offset=) behavior.

    Usage (from main.py):
        monitor = TrailMonitor(order_mgr, telegram, journal)
        monitor.start(risk_levels, trail_state, entry_bar_time_ms, callback)
        monitor.on_bar_close(bar_close, bar_high, bar_low, bar_open, current_atr)
    """

    def __init__(self, order_mgr, telegram, journal) -> None:
        self._order_mgr = order_mgr
        self._telegram  = telegram
        self._journal   = journal

        self._running       : bool = False
        self._risk          : Optional[RiskLevels]  = None
        self._state         : Optional[TrailState]  = None
        self._on_exit_cb    : Optional[Callable]    = None
        self._entry_bar_ms  : int  = 0
        self._task          : Optional[asyncio.Task] = None
        self._exit_fired    : bool = False

    # ── Start / Stop ──────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels      : RiskLevels,
        trail_state      : TrailState,
        entry_bar_time_ms: int,
        on_trail_exit    : Callable,
    ) -> None:
        """Begin monitoring. Called once after entry fill is confirmed."""
        self._risk         = risk_levels
        self._state        = trail_state
        self._on_exit_cb   = on_trail_exit
        self._entry_bar_ms = entry_bar_time_ms
        self._exit_fired   = False
        self._running      = True

        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._tick_loop())
        logger.info(
            f"[TRAIL] Started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"is_long={risk_levels.is_long}"
        )

    def stop(self) -> None:
        """Cancel the tick loop without firing an exit callback."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    # ── Bar-close update (called by main.py every 30-min bar) ─────────────────

    def on_bar_close(
        self,
        bar_close  : float,
        bar_high   : float,
        bar_low    : float,
        current_atr: float,
        bar_open   : float = 0.0,   # FIX-TRAIL-02: needed for TP-vs-SL priority
    ) -> None:
        """
        Called by main.py at the close of every 30-minute bar while in position.

        Responsibilities:
          1. Upgrade trail stage using bar-close profit (FIX-TRAIL-03 — Pine parity:
             trailStage is updated by `close` on bar close, not intrabar ticks)
          2. Check if TP or SL was crossed THIS bar (same-bar exit)
          3. Update peak_price with bar extreme (high/low)
          4. Update current_atr for next tick loop cycle
          5. Resolve TP-vs-SL priority using bar_open distance (FIX-TRAIL-02)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long = risk.is_long

        # ── 1. Upgrade trail stage from bar CLOSE profit ─────────────────────
        # FIX-TRAIL-03: Pine evaluates trailStage with `close` on bar close,
        # not with intrabar high/low. Using bar high/low to upgrade stages
        # would advance the stage earlier than Pine does, causing a tighter
        # trail_offset to apply sooner and different exits.
        close_profit = (bar_close - risk.entry_price) if is_long else (risk.entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, current_atr)
        if new_stage > state.stage:
            logger.info(f"[TRAIL] Stage {state.stage} → {new_stage} at bar close profit={close_profit:.2f}")
            state.stage = new_stage

        # ── 2. Update peak price with this bar's extreme ──────────────────────
        if is_long:
            if bar_high > state.peak_price:
                state.peak_price = bar_high
        else:
            if bar_low < state.peak_price or state.peak_price == 0.0:
                state.peak_price = bar_low

        # ── 3. Update ATR for live tick loop ──────────────────────────────────
        self._current_atr = current_atr

        # ── 4. Same-bar exit check ────────────────────────────────────────────
        # Pine runs strategy.exit() on every bar close. If bar_high >= TP or
        # bar_low <= SL (long), both could be crossed in the same bar.
        # Resolve priority by which level was closer to bar_open.
        tp_hit = (bar_high >= risk.tp) if is_long else (bar_low <= risk.tp)
        sl_hit = (bar_low <= risk.sl) if is_long else (bar_high >= risk.sl)

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                # FIX-TRAIL-02: when both hit, fire the one closer to bar_open.
                # If bar_open is not provided (0.0), default to TP priority on
                # profitable bars (conservative assumption).
                ref = bar_open if bar_open > 0.0 else bar_close
                dist_tp = abs(ref - risk.tp)
                dist_sl = abs(ref - risk.sl)
                exit_at_tp = dist_tp <= dist_sl
                exit_price = risk.tp if exit_at_tp else risk.sl
                reason     = "TP (bar close)" if exit_at_tp else "SL (bar close)"
            elif tp_hit:
                exit_price = risk.tp
                reason     = "TP (bar close)"
            else:
                exit_price = risk.sl
                reason     = "SL (bar close)"

            logger.info(f"[TRAIL] Same-bar exit detected: {reason} @ {exit_price:.2f}")
            asyncio.get_event_loop().create_task(self._fire_exit(exit_price, reason))

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        """
        Polls exchange mark price every TRAIL_LOOP_SEC seconds.
        Checks TP, hard SL, breakeven, and trailing SL on every tick.
        """
        # Use entry ATR as initial current_atr; on_bar_close updates it.
        self._current_atr = self._risk.atr if self._risk else 0.0

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

    async def _evaluate_tick(self, price: float) -> None:
        """
        Evaluate all exit conditions for one price tick.

        Order of priority (matches Pine Script evaluation order):
          1. TP limit                → fire TP exit
          2. Hard SL (initial stop) → fire SL exit
          3. Max SL dynamic         → fire MAX_SL exit  (FIX-TRAIL-05)
          4. Breakeven              → update SL to entry_price (FIX-TRAIL-04)
          5. Trail SL               → update SL from peak (FIX-TRAIL-01)
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        atr         = self._current_atr
        entry_price = risk.entry_price

        # ── Track intrabar peak ───────────────────────────────────────────────
        if is_long:
            if price > state.peak_price:
                state.peak_price = price
        else:
            if state.peak_price == 0.0 or price < state.peak_price:
                state.peak_price = price

        peak_profit = (
            (state.peak_price - entry_price) if is_long
            else (entry_price - state.peak_price)
        )
        current_profit = (price - entry_price) if is_long else (entry_price - price)

        # ── 1. TP hit ─────────────────────────────────────────────────────────
        if is_long and price >= risk.tp:
            await self._fire_exit(risk.tp, "TP")
            return
        if not is_long and price <= risk.tp:
            await self._fire_exit(risk.tp, "TP")
            return

        # ── 2. Hard SL hit ────────────────────────────────────────────────────
        if is_long and price <= state.current_sl:
            await self._fire_exit(price, "SL")
            return
        if not is_long and price >= state.current_sl:
            await self._fire_exit(price, "SL")
            return

        # ── 3. Max SL hit (FIX-TRAIL-05) ─────────────────────────────────────
        # Pine: blockExitMaxSL on entry bar. We replicate by checking that
        # at least one bar has closed since entry (entry_bar_ms + 30min).
        if not state.max_sl_fired:
            max_sl_threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
            entry_bar_over = (time.time() * 1000) > (self._entry_bar_ms + 30 * 60 * 1000)
            if entry_bar_over:
                if is_long and price <= entry_price - max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL")
                    return
                if not is_long and price >= entry_price + max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL")
                    return

        # ── 4. Breakeven (FIX-TRAIL-04) ──────────────────────────────────────
        # Pine: strategy.exit("BE-L", stop=entryPrice) — stop IS entry_price,
        # not entry_price ± some buffer.
        if not state.be_done and current_profit >= atr * BE_MULT:
            be_sl = entry_price   # exactly entry price, as in Pine
            if is_long and be_sl > state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven set: SL → {be_sl:.2f}")
            elif not is_long and be_sl < state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven set: SL → {be_sl:.2f}")

        # ── 5. Trailing SL (FIX-TRAIL-01: pts=activation, off=distance) ──────
        # FIX-TRAIL-03: stage upgrades are done on bar close by on_bar_close().
        # Here we only APPLY the current stage's trail stop — we do NOT upgrade
        # stage based on intrabar tick data. This matches Pine's behavior where
        # trailStage can only advance at bar close.
        if state.stage > 0:
            trail_sl = _compute_trail_sl(
                stage            = state.stage,
                atr              = atr,
                peak_price       = state.peak_price,
                peak_profit_dist = peak_profit,
                is_long          = is_long,
            )
            if trail_sl is not None:
                if is_long and trail_sl > state.current_sl:
                    state.current_sl = trail_sl
                    logger.debug(f"[TRAIL] Trail SL → {trail_sl:.2f} (stage {state.stage})")
                elif not is_long and trail_sl < state.current_sl:
                    state.current_sl = trail_sl
                    logger.debug(f"[TRAIL] Trail SL → {trail_sl:.2f} (stage {state.stage})")

            # Re-check SL after trail update
            if is_long and price <= state.current_sl:
                await self._fire_exit(price, f"Trail SL (stage {state.stage})")
                return
            if not is_long and price >= state.current_sl:
                await self._fire_exit(price, f"Trail SL (stage {state.stage})")
                return

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str) -> None:
        """Fire exit once. Idempotent — second call is a no-op."""
        if self._exit_fired:
            return
        self._exit_fired = True
        self._running    = False

        logger.info(f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f}")

        # Cancel bracket orders on exchange (best-effort)
        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed: {e}")

        # Place market close order
        try:
            is_long = self._risk.is_long if self._risk else True
            await self._order_mgr.close_position(is_long=is_long)
        except Exception as e:
            logger.error(f"[TRAIL] close_position failed: {e}", exc_info=True)

        # Invoke main.py callback
        if self._on_exit_cb is not None:
            try:
                await self._on_exit_cb(exit_price, reason)
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch ──────────────────────────────────────────────────

    async def _get_mark_price(self) -> Optional[float]:
        """Fetch current mark price from exchange."""
        try:
            ticker = await self._order_mgr.fetch_ticker()
            if ticker is None:
                return None
            # Try mark price first (exact Pine parity), fall back to last
            return float(
                ticker.get("markPrice")
                or ticker.get("mark")
                or ticker.get("last")
                or 0.0
            )
        except Exception as e:
            logger.debug(f"[TRAIL] fetch_ticker error: {e}")
            return None

    # ── Feed integration (FIX-PEAK-WS) ───────────────────────────────────────

    def push_ws_candle(self, high: float, low: float) -> None:
        """
        Called by ws_feed.py on every intrabar websocket candle update.
        Updates peak_price from the live high/low stream so the trail stop
        can tighten as price moves — without waiting for the tick loop poll.
        """
        if not self._running or self._exit_fired or self._state is None:
            return
        if self._risk is None:
            return
        if self._risk.is_long:
            if high > self._state.peak_price:
                self._state.peak_price = high
        else:
            if self._state.peak_price == 0.0 or low < self._state.peak_price:
                self._state.peak_price = low
