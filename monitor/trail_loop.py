"""
monitor/trail_loop.py — Shiva Sniper v10 — Pine Script Exact Exit Parity
════════════════════════════════════════════════════════════════════════════

DIVERGENCE FIXES IN THIS VERSION (root causes of exit timing mismatch):
──────────────────────────────────────────────────────────────────────────

FIX-EXIT-01 | CRITICAL — Trail stage upgraded intrabar from tick data.
  Pine Script: trailStage is a `var` updated with `close` (bar-close price).
  FIX: _upgrade_stage() is now called ONLY in on_bar_close(), using bar
  close price. The tick loop applies only the current stage.

FIX-EXIT-02 | CRITICAL — SL recomputed with live ATR on every tick.
  Pine Script: strategy.exit() uses the ATR from the entry bar (frozen).
  FIX: Trail offset distance computed from risk.atr (entry-bar ATR only).
  Max SL still uses live ATR (FIX-EXIT-03).

FIX-EXIT-03 | MEDIUM — Max SL uses live ATR (intentional — Pine parity).
  Pine: maxSLthresh = math.min(atr * maxSLmult, maxSLPoints) — current bar ATR.

FIX-EXIT-04 | MEDIUM — TP/SL same-bar priority resolved by bar_open distance.
  Pine: broker emulator fires whichever was closer to bar_open.
  FIX: bar_open parameter used for priority resolution.

FIX-EXIT-05 | LOW — Breakeven activation moved to on_bar_close() (Pine parity).
  Pine: BE fires on bar close (close - entryPrice > beTrigger), not intrabar.
  FIX: BE activated in on_bar_close() only; tick loop enforces the SL level.

FIX-EXIT-06 | LOW — entry_bar_ms check uses BAR_PERIOD_MS (not hardcoded 30m).
  FIX: Uses CANDLE_TIMEFRAME to compute BAR_PERIOD_MS dynamically.

════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from config import (
    TRAIL_STAGES, BE_MULT, MAX_SL_MULT, MAX_SL_POINTS,
    TRAIL_LOOP_SEC, BRACKET_SL_BUFFER, TRAIL_SL_PRE_FIRE_BUFFER,
    CANDLE_TIMEFRAME,
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
    return 1_800_000  # fallback 30m

BAR_PERIOD_MS = _tf_to_ms(CANDLE_TIMEFRAME)  # FIX-EXIT-06


# ─── Pine parity helpers ───────────────────────────────────────────────────────

def _upgrade_stage(current_stage: int, profit_dist: float, atr: float) -> int:
    """
    Returns the highest trail stage unlocked by profit_dist.
    Stages only upgrade, never downgrade — matches Pine's `var trailStage`.

    IMPORTANT: Only call this from on_bar_close() with bar-close profit_dist,
    NOT from the tick loop with intrabar price. (FIX-EXIT-01)
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


def _compute_trail_sl(
    stage: int,
    entry_atr: float,       # FIX-EXIT-02: frozen entry-bar ATR, not live ATR
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
) -> Optional[float]:
    """
    Returns the trailing stop level, or None if not yet activated.

    Pine Script mapping:
      trail_points  -> ACTIVATION threshold (profit must reach this)   (pts_mult)
      trail_offset  -> DISTANCE stop is placed from the peak           (off_mult)

    FIX-EXIT-02: Uses entry_atr (frozen at entry bar) for trail distance.
    """
    if stage == 0:
        return None

    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]

    activation = entry_atr * pts_mult   # profit must be >= this
    offset     = entry_atr * off_mult   # stop placed this far from peak

    if peak_profit_dist < activation:
        return None  # trail not yet activated for this stage

    return (peak_price - offset) if is_long else (peak_price + offset)


def _check_be(current_profit: float, entry_atr: float) -> bool:
    """Returns True if breakeven should activate (mirrors Pine's beTrigger check)."""
    return current_profit > entry_atr * BE_MULT


# ─── TrailMonitor ──────────────────────────────────────────────────────────────

class TrailMonitor:
    """
    Tick-resolution trailing stop monitor with exact Pine Script parity.

    on_bar_close() -> stage upgrade + BE check + same-bar exit detection
    _tick_loop()   -> TP / SL / trail enforcement at tick resolution
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

        # FIX-EXIT-03: separate current_atr for Max SL (live ATR, Pine parity)
        # vs entry_atr frozen at entry for trail offset (FIX-EXIT-02)
        self._current_atr   : float = 0.0   # updated each bar — for Max SL only

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

        # FIX-EXIT-02: freeze entry-bar ATR — trail offset never changes after entry
        self._current_atr  = risk_levels.atr   # initialise; updated by on_bar_close

        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._tick_loop())
        logger.info(
            f"[TRAIL] Started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"entry_atr={risk_levels.atr:.2f} is_long={risk_levels.is_long}"
        )

    def stop(self) -> None:
        """Cancel the tick loop without firing an exit callback."""
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
        bar_open   : float,       # FIX-EXIT-04: required for TP-vs-SL priority
        current_atr: float,       # FIX-EXIT-03: live ATR for Max SL check
    ) -> None:
        """
        Called by main.py at the close of every candle bar while in position.

        Pine parity responsibilities:
          1. Upgrade trail stage using bar-CLOSE profit (FIX-EXIT-01)
          2. Check Breakeven activation from bar-close profit (FIX-EXIT-05)
          3. Update peak_price with bar extreme (high/low)
          4. Update live ATR for Max SL (FIX-EXIT-03)
          5. Same-bar exit check (TP and/or SL hit this bar)
          6. Resolve TP-vs-SL priority via bar_open distance (FIX-EXIT-04)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        entry_atr   = risk.atr          # FIX-EXIT-02: frozen entry-bar ATR

        # FIX-EXIT-03: update live ATR for Max SL calculation only
        self._current_atr = current_atr

        # ── 1. Upgrade trail stage from bar CLOSE profit ─────────────────────
        # FIX-EXIT-01: Pine's trailStage uses `close` (bar-close), not intrabar.
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, entry_atr)
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} -> {new_stage} | "
                f"bar_close_profit={close_profit:.2f} entry_atr={entry_atr:.2f}"
            )
            state.stage = new_stage

        # ── 2. Breakeven check from bar CLOSE profit (FIX-EXIT-05) ───────────
        # Pine: if strategy.position_size > 0 and close - entryPrice > beTrigger
        if not state.be_done and _check_be(close_profit, entry_atr):
            be_sl = entry_price   # Pine: stop=entryPrice exactly
            if is_long and be_sl > state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven activated at bar close: SL -> {be_sl:.2f}")
            elif not is_long and be_sl < state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven activated at bar close: SL -> {be_sl:.2f}")

        # ── 3. Update peak price with this bar's high/low ─────────────────────
        if is_long:
            if bar_high > state.peak_price:
                state.peak_price = bar_high
        else:
            if state.peak_price == 0.0 or bar_low < state.peak_price:
                state.peak_price = bar_low

        # ── 4. Same-bar exit check ────────────────────────────────────────────
        # Use state.current_sl (may have moved to BE or trail) not initial risk.sl
        tp_hit = (bar_high >= risk.tp)          if is_long else (bar_low  <= risk.tp)
        sl_hit = (bar_low  <= state.current_sl) if is_long else (bar_high >= state.current_sl)

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                # FIX-EXIT-04: resolve by which was closer to bar_open
                ref      = bar_open if bar_open > 0.0 else bar_close
                dist_tp  = abs(ref - risk.tp)
                dist_sl  = abs(ref - state.current_sl)
                use_tp   = dist_tp <= dist_sl
                exit_px  = risk.tp           if use_tp else state.current_sl
                reason   = "TP (bar close)" if use_tp else "SL (bar close)"
            elif tp_hit:
                exit_px = risk.tp
                reason  = "TP (bar close)"
            else:
                exit_px = state.current_sl
                reason  = "SL (bar close)"

            logger.info(f"[TRAIL] Same-bar exit: {reason} @ {exit_px:.2f}")
            asyncio.get_event_loop().create_task(self._fire_exit(exit_px, reason))

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        """
        Polls exchange mark price every TRAIL_LOOP_SEC seconds.
        NOTE: Stage upgrades and BE activation are NOT done here (FIX-EXIT-01/05).
        """
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

        FIX-EXIT-01: NO stage upgrade here — only in on_bar_close().
        FIX-EXIT-05: NO BE activation here — only in on_bar_close().

        Priority order (matches Pine Script evaluation order):
          1. TP limit
          2. Hard SL / BE SL / Trail SL (current_sl)
          3. Max SL dynamic (uses live ATR per FIX-EXIT-03)
          4. Trail SL update from intrabar peak
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        entry_price = risk.entry_price
        entry_atr   = risk.atr          # FIX-EXIT-02: frozen — never changes

        # ── Track intrabar peak for live trail SL computation ─────────────────
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

        # ── 1. TP hit ─────────────────────────────────────────────────────────
        if is_long and price >= risk.tp:
            await self._fire_exit(risk.tp, "TP")
            return
        if not is_long and price <= risk.tp:
            await self._fire_exit(risk.tp, "TP")
            return

        # ── 2. Hard SL / BE SL / Trail SL ────────────────────────────────────
        if is_long and price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER:
            trail_improved = state.current_sl > risk.sl
            be_at_entry    = state.be_done and abs(state.current_sl - entry_price) < 1e-6
            if trail_improved and not be_at_entry:
                reason = f"Trail SL (stage {state.stage})"
            elif be_at_entry:
                reason = "Breakeven SL"
            else:
                reason = "Initial SL"
            await self._fire_exit(price, reason)
            return
        if not is_long and price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER:
            trail_improved = state.current_sl < risk.sl
            be_at_entry    = state.be_done and abs(state.current_sl - entry_price) < 1e-6
            if trail_improved and not be_at_entry:
                reason = f"Trail SL (stage {state.stage})"
            elif be_at_entry:
                reason = "Breakeven SL"
            else:
                reason = "Initial SL"
            await self._fire_exit(price, reason)
            return

        # ── 3. Max SL (FIX-EXIT-03: live ATR, FIX-EXIT-06: BAR_PERIOD_MS) ────
        if not state.max_sl_fired:
            max_sl_threshold = min(self._current_atr * MAX_SL_MULT, MAX_SL_POINTS)
            entry_bar_over   = (time.time() * 1000) > (self._entry_bar_ms + BAR_PERIOD_MS)
            if entry_bar_over:
                if is_long and price <= entry_price - max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL")
                    return
                if not is_long and price >= entry_price + max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL")
                    return

        # ── 4. Update trailing SL from peak ───────────────────────────────────
        # FIX-EXIT-01: Stage is fixed at whatever on_bar_close() last set.
        # Pine's broker emulator tracks intrabar extremes for offset calc,
        # but STAGE itself only advances at bar close.
        if state.stage > 0:
            trail_sl = _compute_trail_sl(
                stage            = state.stage,
                entry_atr        = entry_atr,    # FIX-EXIT-02: frozen ATR
                peak_price       = state.peak_price,
                peak_profit_dist = peak_profit,
                is_long          = is_long,
            )
            if trail_sl is not None:
                if is_long and trail_sl > state.current_sl:
                    state.current_sl = trail_sl
                    logger.debug(f"[TRAIL] Trail SL -> {trail_sl:.2f} (stage {state.stage})")
                elif not is_long and trail_sl < state.current_sl:
                    state.current_sl = trail_sl
                    logger.debug(f"[TRAIL] Trail SL -> {trail_sl:.2f} (stage {state.stage})")

            # Re-check SL after trail update
            if is_long and price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER:
                await self._fire_exit(price, f"Trail SL (stage {state.stage})")
                return
            if not is_long and price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER:
                await self._fire_exit(price, f"Trail SL (stage {state.stage})")
                return

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str) -> None:
        """
        Fire exit once. Idempotent on success.

        CRITICAL FIX: previously, _exit_fired was set to True and _on_exit_cb
        was called UNCONDITIONALLY — even if close_position() raised (the old
        close_position had signature (self, reason=...) and this method calls
        it with is_long=... which raised TypeError, silently swallowed). The
        bot would then reset in_position=False while the exchange position
        stayed open — classic "bot thinks flat, exchange still long" desync.

        New behaviour:
          1. Set _exit_fired so re-entry into this method is blocked
             during the in-flight close.
          2. cancel_all_orders() — best-effort, warnings only.
          3. close_position() — MUST succeed. On failure, reset _exit_fired
             so the next tick/bar retries, and DO NOT call _on_exit_cb
             (which would reset bot.in_position=False and strand the position).
          4. Only on success: set _running=False and call _on_exit_cb.
        """
        if self._exit_fired:
            return
        self._exit_fired = True   # block concurrent re-entry while close is in flight

        logger.info(f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f}")

        # 1. Cancel any standing orders (no-op in NO-BRACKET mode)
        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed: {e}")

        # 2. Close the position on the exchange (reduce-only market order)
        is_long = self._risk.is_long if self._risk else True
        try:
            await self._order_mgr.close_position(is_long=is_long, reason=reason)
        except Exception as e:
            # DO NOT proceed to callback — bot state must stay in_position=True
            # so the next tick re-attempts. Reset _exit_fired to allow retry.
            logger.error(
                f"[TRAIL] close_position failed — will retry next tick: {e}",
                exc_info=True,
            )
            self._exit_fired = False
            return

        # 3. Exchange close succeeded — stop the monitor and notify bot to reset state
        self._running = False
        if self._on_exit_cb is not None:
            try:
                await self._on_exit_cb(exit_price, reason)
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch ──────────────────────────────────────────────────

    async def _get_mark_price(self) -> Optional[float]:
        """Fetch current mark price from exchange (mark > last for Pine parity)."""
        try:
            ticker = await self._order_mgr.fetch_ticker()
            if ticker is None:
                return None
            return float(
                ticker.get("markPrice")
                or ticker.get("mark")
                or ticker.get("last")
                or 0.0
            )
        except Exception as e:
            logger.debug(f"[TRAIL] fetch_ticker error: {e}")
            return None

    # ── Feed integration (live WS candle high/low push) ───────────────────────

    def push_ws_candle(self, high: float, low: float) -> None:
        """
        Called by ws_feed.py on every intrabar websocket candle update.
        Updates peak_price from live high/low so trail SL tightens without
        waiting for the tick poll interval.
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
