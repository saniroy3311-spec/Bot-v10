"""
monitor/trail_loop.py — Shiva Sniper v10 — BUG-FIX-AUDIT-v1
════════════════════════════════════════════════════════════════════════════

FIXES IN THIS VERSION (on top of FIX-EXIT-01 through FIX-EXIT-07):
──────────────────────────────────────────────────────────────────────────

FIX-AUDIT-01 | CATASTROPHIC — _get_mark_price() now correctly extracts
  mark price from the ticker returned by OrderManager.fetch_ticker().
  The old extraction tried ticker.get("markPrice") first, which is the
  ccxt-normalised field. Delta India's ccxt ticker populates it, but we
  now also try ticker["info"]["mark_price"] as a fallback for robustness.
  Root cause of BUG-7: fetch_ticker didn't exist → None returned → tick
  loop was a complete no-op. Fixed in orders/manager.py (FIX-OM-001).

FIX-AUDIT-02 | MEDIUM — asyncio.get_event_loop() replaced with
  asyncio.get_running_loop() throughout.
  get_event_loop() from a sync function emits DeprecationWarning in
  Python 3.10+ and raises RuntimeError in 3.12+ if no loop is running.
  on_bar_close() and start() are always called from within asyncio.run(),
  so get_running_loop() is always valid and is the correct API.

FIX-AUDIT-03 | HIGH — _fire_exit() now carries a `source` tag:
    "bar_close"  → same-bar exit detected in on_bar_close()
    "tick"       → intrabar exit from _tick_loop() or Max SL check
  The source is passed to the on_trail_exit callback. main.py uses it
  to decide whether to consume _pending_signal (same-bar only) or discard
  it (tick-loop exit — the snap would be stale from the previous bar close).

FIX-AUDIT-04 | MEDIUM — Max SL entry-bar block uses candle boundary,
  not wall-clock duration from fill time.
  Pine: blockExitMaxSL fires for the remainder of the current candle.
  Old Python: blocked for BAR_PERIOD_MS from fill time regardless of
  where in the candle the fill occurred. If filled at 09:25 on a 30m bar,
  Pine unblocks at 09:30, bot unblocked at 09:55 (25 min too late).
  Fix: _entry_bar_end_ms = floor(fill_time / period_ms) * period_ms + period_ms

PRESERVED FROM FIX-EXIT-01 through FIX-EXIT-07 (unchanged):
  - Stage upgrades only in on_bar_close() (FIX-EXIT-01)
  - Frozen entry-bar ATR for trail offset (FIX-EXIT-02)
  - Live ATR for Max SL (FIX-EXIT-03)
  - Bar-open distance for TP-vs-SL priority (FIX-EXIT-04)
  - BE activation only at bar close (FIX-EXIT-05)
  - BAR_PERIOD_MS computed from CANDLE_TIMEFRAME (FIX-EXIT-06)
  - Trail computed at stage 0 using stage-1 params (FIX-EXIT-07)
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
    entry_atr: float,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
) -> Optional[float]:
    """
    Returns the trailing stop level, or None if not yet activated.

    stage==0 uses stage-1 params (FIX-EXIT-07).
    Uses entry_atr (frozen at entry bar) for trail distance (FIX-EXIT-02).
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]

    activation = entry_atr * pts_mult
    offset     = entry_atr * off_mult

    if peak_profit_dist < activation:
        return None

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
        self._entry_bar_end_ms: int = 0  # FIX-AUDIT-04: candle boundary end
        self._task          : Optional[asyncio.Task] = None
        self._exit_fired    : bool = False

        self._current_atr   : float = 0.0   # FIX-EXIT-03: live ATR for Max SL

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
        self._current_atr  = risk_levels.atr

        # FIX-AUDIT-04: compute the end of the current candle boundary.
        # Pine blocks Max SL for the remainder of the CURRENT candle, not for
        # BAR_PERIOD_MS from fill time. floor(fill_ms / period_ms) gives the
        # candle open boundary; adding period_ms gives the candle close time.
        self._entry_bar_end_ms = (
            (entry_bar_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        ) + BAR_PERIOD_MS

        # FIX-AUDIT-02: use get_running_loop() — always valid here since start()
        # is called from async _enter() which runs inside asyncio.run().
        self._task = asyncio.get_running_loop().create_task(self._tick_loop())
        logger.info(
            f"[TRAIL] Started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"entry_atr={risk_levels.atr:.2f} is_long={risk_levels.is_long} | "
            f"candle_unblock_at={self._entry_bar_end_ms}"
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
        bar_open   : float,
        current_atr: float,
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

        FIX-AUDIT-02: same-bar exit task uses asyncio.get_running_loop()
        FIX-AUDIT-03: same-bar exit passes source="bar_close" to _fire_exit()
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        entry_atr   = risk.atr

        # FIX-EXIT-03: update live ATR for Max SL calculation only
        self._current_atr = current_atr

        # ── 1. Upgrade trail stage from bar CLOSE profit ─────────────────────
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, entry_atr)
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} -> {new_stage} | "
                f"bar_close_profit={close_profit:.2f} entry_atr={entry_atr:.2f}"
            )
            state.stage = new_stage

        # ── 2. Breakeven check from bar CLOSE profit (FIX-EXIT-05) ───────────
        if not state.be_done and _check_be(close_profit, entry_atr):
            be_sl = entry_price
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

        # ── 3b. Recompute trail SL from bar extreme (safety net) ──────────────
        if is_long:
            bar_peak_profit = bar_high - entry_price
            _bar_trail_sl = _compute_trail_sl(state.stage, entry_atr, bar_high, bar_peak_profit, True)
            if _bar_trail_sl is not None and _bar_trail_sl > state.current_sl:
                state.current_sl = _bar_trail_sl
                logger.debug(f"[TRAIL] Bar-close trail SL update → {_bar_trail_sl:.2f} (stage {state.stage})")
        else:
            bar_peak_profit = entry_price - bar_low
            _bar_trail_sl = _compute_trail_sl(state.stage, entry_atr, bar_low, bar_peak_profit, False)
            if _bar_trail_sl is not None and _bar_trail_sl < state.current_sl:
                state.current_sl = _bar_trail_sl
                logger.debug(f"[TRAIL] Bar-close trail SL update → {_bar_trail_sl:.2f} (stage {state.stage})")

        # ── 4. Same-bar exit check ────────────────────────────────────────────
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
            # FIX-AUDIT-02: get_running_loop() instead of get_event_loop()
            # FIX-AUDIT-03: source="bar_close" so main.py knows to consume _pending_signal
            asyncio.get_running_loop().create_task(
                self._fire_exit(exit_px, reason, source="bar_close")
            )

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
        entry_atr   = risk.atr

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
            await self._fire_exit(risk.tp, "TP", source="tick")
            return
        if not is_long and price <= risk.tp:
            await self._fire_exit(risk.tp, "TP", source="tick")
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
            await self._fire_exit(price, reason, source="tick")
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
            await self._fire_exit(price, reason, source="tick")
            return

        # ── 3. Max SL (FIX-EXIT-03 + FIX-AUDIT-04) ───────────────────────────
        # FIX-AUDIT-04: use _entry_bar_end_ms (candle boundary end) instead of
        # _entry_bar_ms + BAR_PERIOD_MS (wall-clock from fill time).
        # Pine: block Max SL for the remainder of the CURRENT candle only.
        if not state.max_sl_fired:
            max_sl_threshold = min(self._current_atr * MAX_SL_MULT, MAX_SL_POINTS)
            entry_bar_over   = (time.time() * 1000) >= self._entry_bar_end_ms  # FIX-AUDIT-04
            if entry_bar_over:
                if is_long and price <= entry_price - max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return
                if not is_long and price >= entry_price + max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return

        # ── 4. Update trailing SL from peak (ALL stages, including 0) ────────
        trail_sl = _compute_trail_sl(
            stage            = state.stage,
            entry_atr        = entry_atr,
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
            await self._fire_exit(price, f"Trail SL (stage {state.stage})", source="tick")
            return
        if not is_long and price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER:
            await self._fire_exit(price, f"Trail SL (stage {state.stage})", source="tick")
            return

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """
        Fire exit once. Idempotent on success.

        FIX-AUDIT-03: Added `source` parameter.
          "bar_close" → same-bar detection from on_bar_close()
          "tick"      → intrabar detection from _tick_loop() / Max SL
        source is forwarded to the on_trail_exit callback so main.py can
        decide whether to consume _pending_signal or discard it.

        On close_position() failure: reset _exit_fired so the next tick
        retries. Do NOT call _on_exit_cb (prevents false in_position=False).
        """
        if self._exit_fired:
            return
        self._exit_fired = True

        logger.info(f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f} source={source}")

        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed: {e}")

        is_long = self._risk.is_long if self._risk else True
        try:
            await self._order_mgr.close_position(is_long=is_long, reason=reason)
        except Exception as e:
            logger.error(
                f"[TRAIL] close_position failed — will retry next tick: {e}",
                exc_info=True,
            )
            self._exit_fired = False
            return

        self._running = False
        if self._on_exit_cb is not None:
            try:
                await self._on_exit_cb(exit_price, reason, source)  # FIX-AUDIT-03
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch ──────────────────────────────────────────────────

    async def _get_mark_price(self) -> Optional[float]:
        """
        Fetch current mark price from exchange.

        FIX-AUDIT-01: The old implementation called self._order_mgr.fetch_ticker()
        which did not exist on OrderManager (AttributeError silently swallowed →
        returned None every tick → _evaluate_tick() never called → all intrabar
        exits dead). Fixed in orders/manager.py (FIX-OM-001).

        Delta India ticker key priority:
          1. ticker["markPrice"]              — ccxt-normalised field
          2. ticker["info"]["mark_price"]     — raw exchange field (fallback)
          3. ticker["last"]                   — last traded price (last resort)
        """
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
            if price > 0:
                return price
            logger.warning(f"[TRAIL] Ticker returned no usable price: keys={list(ticker.keys())}")
            return None
        except Exception as e:
            logger.warning(f"[TRAIL] _get_mark_price failed: {e}")
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
