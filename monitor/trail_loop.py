"""
monitor/trail_loop.py — Shiva Sniper v10 — PINE-STAGE-EXACT
════════════════════════════════════════════════════════════════════════════

NEW IN THIS VERSION (FIX-STAGE0-PINE-PARITY v10.2):
──────────────────────────────────────────────────────────────────────────
Pine Script has NO stage 0 trailing. Trail only starts after stage 1
trigger is hit (profit_dist >= ATR × 1.0). Before that, only the fixed
original SL is active.

Previous bot code used `max(stage - 1, 0)` in _compute_trail_sl(), which
meant stage 0 used TRAIL_STAGES[0] — i.e., trail was active from the very
first tick. This caused:
  • Immediate trail SL computed from bar_low (barely moved for a short)
  • Same-bar exit fired because bar_high crossed the newly computed trail SL
  • Trade killed on first bar close with near-zero profit

Fix applied in two places:
  1. _compute_trail_sl(): return None when stage == 0
     → fixed SL remains active, no trail computed yet
  2. on_bar_close() same-bar exit check: use risk.sl (fixed) when stage == 0
     → prevents false same-bar exit from trail SL computed this same bar

Result: bot now holds trades like Pine — fixed SL active until 1× ATR
profit is captured, then trail takes over.

NEW IN PREVIOUS VERSION (FIX-PINE-MINTICK v10.1):
──────────────────────────────────────────────────────────────────────────
PINE_MINTICK removed from _compute_trail_sl() activation and offset.

  The Pine script passes RAW USD point values to strategy.exit():
    activePts = atr * trail1Pts   (e.g. 310 * 0.70 = 217 pts)
    activeOff = atr * trail1Off   (e.g. 310 * 0.55 = 170 pts)
    strategy.exit(..., trail_points=activePts, trail_offset=activeOff)

  Previous bot code applied PINE_MINTICK (0.1) scaling — WRONG:
    activation = atr * pts_mult * 0.1  → Stage 1 at  21.7 pts
    offset     = atr * off_mult * 0.1  → SL only 17 pts from peak

  Corrected bot code (no PINE_MINTICK):
    activation = atr * pts_mult        → Stage 1 at 217 pts
    offset     = atr * off_mult        → SL 170 pts from peak

  Effect: bot was exiting within seconds of every entry. Now holds like Pine.

PINE-STAGE-EXACT (preserved):
──────────────────────────────────────────────────────────────────────────
Stage upgrade triggers use raw ATR multiples — no PINE_MINTICK scaling.
  profit_dist >= live_atr * trigger_mult   ← correct (unchanged)

────────────────────────────────────────────────────────────────────────
ALL PREVIOUS FIXES PRESERVED (unchanged):
────────────────────────────────────────────────────────────────────────
FIX-PARITY-v6 (FIX-INTRABAR + FIX-PUSH-SL)
FIX-INTRABAR-01: stage upgrade in _evaluate_tick() using peak_profit.
FIX-INTRABAR-02: BE activation in _evaluate_tick() using peak_profit.
FIX-BRACKET-CHURN: bracket placed once, never amended.
FIX-TRAIL-05: correct exit reason labels in re-check path.
FIX-TRAIL-04: close_position retries capped at 3, no infinite cascade.
FIX-PARITY-01: trail uses live_atr (bar-close ATR), not frozen entry_atr.
FIX-PARITY-02: WS price push replaces REST polling as primary exit path.
FIX-PARITY-03: push_ws_candle schedules immediate TP/SL evaluation.
FIX-DUAL-SOURCE-B: Binance/Delta price-source offset compensation.
FIX-FILL-PRICE: actual exchange fill used for journal/Telegram P&L.
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

def _upgrade_stage(current_stage: int, profit_dist: float, live_atr: float) -> int:
    """
    Returns the highest trail stage unlocked by profit_dist.
    Stages only upgrade, never downgrade — matches Pine's `var trailStage`.

    PINE-STAGE-EXACT: trigger uses raw ATR multiples, NO PINE_MINTICK.
      Pine: if profitDist >= atr * trailXTrigger
      Old bot: profit_dist >= live_atr * trigger_mult * PINE_MINTICK  ← WRONG
      New bot: profit_dist >= live_atr * trigger_mult                 ← CORRECT

    PINE_MINTICK is only for trail_points/trail_offset distances (tick units).
    The stage trigger is a price comparison — mintick must not be applied.

    FIX-INTRABAR-01 (preserved): called from both on_bar_close() AND
    _evaluate_tick() so stages ratchet on every tick the moment peak_profit
    crosses a threshold, not only at bar close.
    """
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        # PINE-STAGE-EXACT: raw ATR multiple, no PINE_MINTICK scaling
        if profit_dist >= live_atr * trigger_mult:
            candidate = i + 1
            if candidate > new_stage:
                new_stage = candidate
            break
    return new_stage


def _compute_trail_sl(
    stage: int,
    live_atr: float,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
) -> Optional[float]:
    """
    Returns the trailing stop level, or None if not yet activated.

    FIX-PARITY-01: uses live_atr (current bar's ATR), not frozen entry_atr.

    FIX-PINE-MINTICK (v10.1):
    Pine's strategy.exit(trail_points=P, trail_offset=O) in this script passes
    RAW USD point values (activePts = atr * trailXPts, activeOff = atr * trailXOff)
    — NOT tick-unit values. The script never divides by syminfo.mintick before
    passing to strategy.exit(), so PINE_MINTICK must NOT be applied here.

    Confirmed from Pine script:
        activePts = atr * trail1Pts   (e.g. 310 * 0.70 = 217 pts)
        activeOff = atr * trail1Off   (e.g. 310 * 0.55 = 170 pts)
        strategy.exit(..., trail_points=activePts, trail_offset=activeOff)

    Previous code multiplied by PINE_MINTICK (0.1), making activation 10x too
    early (21.7 pts instead of 217 pts) — causing exits within seconds of entry.

    FIX-STAGE0-PINE-PARITY (v10.2):
    Pine has NO stage 0 trailing. Trail only activates after stage 1 trigger
    (profit_dist >= ATR × trail1Trigger = 1.0 × ATR).
    When stage == 0, return None — use fixed SL only, no trailing yet.
    This prevents the bot from trailing immediately at entry (killing trades
    on the very first bar close with zero profit).

    Correct behaviour:
      • stage == 0  → return None (fixed SL active, no trail yet)
      • stage >= 1  → trail activates when peak_profit >= atr * pts_mult
      • TRAIL SL:   peak - (atr * off_mult)  [long]
                    peak + (atr * off_mult)  [short]
    """
    # FIX-STAGE0-PINE-PARITY: Pine has no stage 0 trail — use fixed SL only
    if stage == 0:
        return None

    idx = stage - 1   # stage 1 → index 0, stage 2 → index 1, etc.
    _, pts_mult, off_mult = TRAIL_STAGES[idx]

    # ACTIVATION: trail arms when peak profit reaches the raw ATR-multiple threshold.
    activation_threshold = live_atr * pts_mult   # FIX-PINE-MINTICK: no * PINE_MINTICK
    if peak_profit_dist < activation_threshold:
        return None

    # Once activated, trail SL sits the raw ATR-multiple offset behind the peak.
    offset = live_atr * off_mult                 # FIX-PINE-MINTICK: no * PINE_MINTICK
    return (peak_price - offset) if is_long else (peak_price + offset)


def _check_be(current_profit: float, live_atr: float) -> bool:
    """
    Returns True if breakeven should activate.

    FIX-PARITY-01: uses live_atr to match Pine's bar-close ATR.
    BE_MULT × ATR is a price comparison (raw pts) — no PINE_MINTICK.
    Pine: beTrigger = atr * beMult  then  close - entryPrice > beTrigger
    """
    return current_profit > live_atr * BE_MULT


# ─── TrailMonitor ──────────────────────────────────────────────────────────────

class TrailMonitor:
    """
    Tick-resolution trailing stop monitor with exact Pine Script parity.

    on_bar_close()    → stage upgrade + BE check + same-bar exit detection
    on_price_tick()   → primary intrabar exit check (called from WS feed)
    _tick_loop()      → 2-second safety-net REST poll (backup only)
    push_ws_candle()  → intrabar peak update + immediate exit eval
    """

    def __init__(self, order_mgr, telegram, journal) -> None:
        self._order_mgr = order_mgr
        self._telegram  = telegram
        self._journal   = journal

        self._running         : bool = False
        self._risk            : Optional[RiskLevels]  = None
        self._state           : Optional[TrailState]  = None
        self._on_exit_cb      : Optional[Callable]    = None
        self._entry_bar_ms    : int  = 0
        self._entry_bar_end_ms: int  = 0   # FIX-AUDIT-04: candle boundary end
        self._task            : Optional[asyncio.Task] = None
        self._exit_fired      : bool = False

        self._current_atr     : float = 0.0   # FIX-PARITY-01: updated in on_bar_close() only

        # FIX-DUAL-SOURCE-B: Binance/Delta price-source offset compensation.
        self._source_offset   : Optional[float] = None
        self._first_tick_ts_ms: int  = 0

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
        self._current_atr  = risk_levels.atr   # seed with entry-bar ATR

        # FIX-DUAL-SOURCE-B: reset offset for new trade.
        self._source_offset    = None
        self._first_tick_ts_ms = 0

        # FIX-AUDIT-04: compute the end of the current candle boundary.
        self._entry_bar_end_ms = (
            (entry_bar_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        ) + BAR_PERIOD_MS

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
          1. Update live ATR (FIX-PARITY-01)
          2. Upgrade trail stage using bar-CLOSE profit (FIX-EXIT-01)
          3. Check Breakeven from bar-close profit (FIX-EXIT-05)
          4. Update peak_price with bar extreme
          5. Recompute trail SL from bar extreme using live ATR
          6. Same-bar exit check (TP and/or SL hit this bar)
          7. Resolve TP-vs-SL priority via bar_open distance (FIX-EXIT-04)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price

        # FIX-DUAL-SOURCE-B: translate Binance bar OHLC to Delta-equivalent.
        if self._source_offset is not None:
            bar_close = bar_close - self._source_offset
            bar_high  = bar_high  - self._source_offset
            bar_low   = bar_low   - self._source_offset
            if bar_open > 0.0:
                bar_open = bar_open - self._source_offset

        # ── 1. Update live ATR (FIX-PARITY-01) ──────────────────────────────
        self._current_atr = current_atr

        # ── 2. Upgrade trail stage from bar CLOSE profit (FIX-EXIT-01) ──────
        # PINE-STAGE-EXACT: _upgrade_stage now uses raw ATR multiples (no PINE_MINTICK)
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, current_atr)
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} -> {new_stage} | "
                f"bar_close_profit={close_profit:.2f} live_atr={current_atr:.2f}"
            )
            state.stage = new_stage

        # ── 3. Breakeven check from bar CLOSE profit (FIX-EXIT-05) ──────────
        if not state.be_done and _check_be(close_profit, current_atr):
            be_sl = entry_price
            if is_long and be_sl > state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven activated: SL -> {be_sl:.2f} (live_atr={current_atr:.2f})")
            elif not is_long and be_sl < state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(f"[TRAIL] Breakeven activated: SL -> {be_sl:.2f} (live_atr={current_atr:.2f})")

        # ── 4. Update peak price with this bar's high/low ─────────────────────
        if is_long:
            if bar_high > state.peak_price:
                state.peak_price = bar_high
        else:
            if state.peak_price == 0.0 or bar_low < state.peak_price:
                state.peak_price = bar_low

        # ── 5. Recompute trail SL from bar extreme using live ATR ─────────────
        # FIX-SAME-BAR-SL: snapshot current_sl BEFORE the trail updates it.
        # Step 6 must check whether this bar's price crossed the SL that was
        # already active at bar-open — NOT the one just derived from this bar's
        # extreme. Without this a SHORT false-exits because:
        #   • trail_sl is computed from bar_low  (price dipped here first)
        #   • state.current_sl is lowered to that new value
        #   • step 6 then checks bar_high >= state.current_sl → TRUE
        #     even though bar_high occurred BEFORE bar_low in the same bar
        # Pine evaluates tick-by-tick so it never has this intrabar race.
        pre_trail_sl = state.current_sl

        if is_long:
            bar_peak_profit = bar_high - entry_price
            _bar_trail_sl = _compute_trail_sl(
                state.stage, current_atr, bar_high, bar_peak_profit, True
            )
            if _bar_trail_sl is not None and _bar_trail_sl > state.current_sl:
                state.current_sl = _bar_trail_sl
                logger.info(
                    f"[TRAIL] Bar-close trail SL -> {_bar_trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={current_atr:.2f})"
                )
        else:
            bar_peak_profit = entry_price - bar_low
            _bar_trail_sl = _compute_trail_sl(
                state.stage, current_atr, bar_low, bar_peak_profit, False
            )
            if _bar_trail_sl is not None and _bar_trail_sl < state.current_sl:
                state.current_sl = _bar_trail_sl
                logger.info(
                    f"[TRAIL] Bar-close trail SL -> {_bar_trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={current_atr:.2f})"
                )

        # ── 6. Same-bar exit check ────────────────────────────────────────────
        # Use pre_trail_sl (the SL active at bar-open) for the sl_hit test.
        # The updated state.current_sl is only valid from the NEXT bar onward.
        #
        # FIX-STAGE0-PINE-PARITY: When stage == 0, no trail has fired yet.
        # Only the original fixed SL (risk.sl) should be used for the check.
        # This prevents a false same-bar exit where the newly computed trail SL
        # (derived from bar_low) is immediately crossed by bar_high in the same bar.
        effective_sl = risk.sl if state.stage == 0 else pre_trail_sl
        tp_hit = (bar_high >= risk.tp)      if is_long else (bar_low  <= risk.tp)
        sl_hit = (bar_low  <= effective_sl) if is_long else (bar_high >= effective_sl)

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                # FIX-EXIT-04: resolve by which was closer to bar_open
                ref      = bar_open if bar_open > 0.0 else bar_close
                dist_tp  = abs(ref - risk.tp)
                dist_sl  = abs(ref - effective_sl)
                use_tp   = dist_tp <= dist_sl
                exit_px  = risk.tp           if use_tp else effective_sl
                reason   = "TP (bar close)" if use_tp else "SL (bar close)"
            elif tp_hit:
                exit_px = risk.tp
                reason  = "TP (bar close)"
            else:
                exit_px = effective_sl
                reason  = "SL (bar close)"

            logger.info(f"[TRAIL] Same-bar exit: {reason} @ {exit_px:.2f}")
            asyncio.get_running_loop().create_task(
                self._fire_exit(exit_px, reason, source="bar_close")
            )

    # ── WS price push — primary exit detection (FIX-PARITY-02) ──────────────

    async def on_price_tick(self, price: float, source: str = "binance") -> None:
        """
        FIX-PARITY-02: Primary intrabar exit detection path.

        Called by ws_feed on every intrabar WS candle update. Zero REST
        calls before the exit decision — matches Pine's tick-level model.

        FIX-DUAL-SOURCE-B: source="binance" prices are translated by the
        captured Binance→Delta offset. source="delta" passes through as-is.
        """
        if not self._running or self._exit_fired or price <= 0:
            return

        if source == "binance" and self._risk is not None:
            if self._source_offset is None:
                raw_offset = price - self._risk.entry_price
                if abs(raw_offset) > 500.0:
                    logger.warning(
                        f"[TRAIL] Source offset rejected (|{raw_offset:+.2f}| > 500): "
                        f"binance_first_tick={price:.2f} "
                        f"delta_fill={self._risk.entry_price:.2f}  "
                        f"will retry on next tick"
                    )
                    return
                self._source_offset    = raw_offset
                self._first_tick_ts_ms = int(time.time() * 1000)
                logger.info(
                    f"[TRAIL] Source offset captured: "
                    f"binance_first_tick={price:.2f} "
                    f"delta_fill={self._risk.entry_price:.2f} "
                    f"offset={self._source_offset:+.2f}  "
                    f"(subsequent Binance ticks corrected to Delta-equivalent space)"
                )
            price = price - self._source_offset

        await self._evaluate_tick(price)

    async def _evaluate_tick_pair(self, tp_side: float, sl_side: float) -> None:
        """
        FIX-PARITY-03: Evaluate TP-side price first, then SL-side.
        Prices are already in Delta-equivalent space (translated by push_ws_candle).
        """
        await self._evaluate_tick(tp_side)
        if not self._exit_fired:
            await self._evaluate_tick(sl_side)

    # ── Internal tick loop — safety net only (FIX-PARITY-02) ─────────────────

    async def _tick_loop(self) -> None:
        """
        FIX-PARITY-02: Demoted to 2-second safety-net REST poll.
        Primary exit detection is via on_price_tick() from the WS feed.
        Stage upgrades and BE activation are NOT done here.
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

        FIX-PARITY-01: trail SL uses self._current_atr (bar-close ATR).
        PINE-STAGE-EXACT: _upgrade_stage uses raw ATR trigger (no PINE_MINTICK).
        FIX-INTRABAR-01: stage upgrades intrabar from peak_profit.
        FIX-INTRABAR-02: BE activates intrabar from peak_profit.

        Priority order:
          1. TP limit
          2. Hard SL / BE SL / Trail SL (current_sl)
          3. Max SL dynamic
          4. Trail SL update from intrabar peak
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
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

        # ── INTRABAR STAGE UPGRADE (FIX-INTRABAR-01) ──────────────────────────
        # PINE-STAGE-EXACT: triggers now use raw ATR multiples (no PINE_MINTICK).
        # Stage 1 now requires 1 full ATR of profit, not 0.1 ATR.
        new_stage = _upgrade_stage(state.stage, peak_profit, self._current_atr)
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} -> {new_stage} (intrabar) | "
                f"peak_profit={peak_profit:.2f} live_atr={self._current_atr:.2f}"
            )
            state.stage = new_stage

        # ── INTRABAR BREAKEVEN ACTIVATION (FIX-INTRABAR-02) ───────────────────
        if not state.be_done and _check_be(peak_profit, self._current_atr):
            be_sl = entry_price
            if is_long and be_sl > state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(
                    f"[TRAIL] Breakeven activated (intrabar): SL -> {be_sl:.2f} "
                    f"(peak_profit={peak_profit:.2f} live_atr={self._current_atr:.2f})"
                )
            elif not is_long and be_sl < state.current_sl:
                state.current_sl = be_sl
                state.be_done    = True
                logger.info(
                    f"[TRAIL] Breakeven activated (intrabar): SL -> {be_sl:.2f} "
                    f"(peak_profit={peak_profit:.2f} live_atr={self._current_atr:.2f})"
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
        if not state.max_sl_fired:
            max_sl_threshold = min(self._current_atr * MAX_SL_MULT, MAX_SL_POINTS)
            entry_bar_over   = (time.time() * 1000) >= self._entry_bar_end_ms
            if entry_bar_over:
                if is_long and price <= entry_price - max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return
                if not is_long and price >= entry_price + max_sl_threshold:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return

        # ── 4. Update trailing SL from peak using live ATR (FIX-PARITY-01) ───
        trail_sl = _compute_trail_sl(
            stage            = state.stage,
            live_atr         = self._current_atr,
            peak_price       = state.peak_price,
            peak_profit_dist = peak_profit,
            is_long          = is_long,
        )
        if trail_sl is not None:
            if is_long and trail_sl > state.current_sl:
                state.current_sl = trail_sl
                logger.info(
                    f"[TRAIL] Trail SL -> {trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={self._current_atr:.2f})"
                )
            elif not is_long and trail_sl < state.current_sl:
                state.current_sl = trail_sl
                logger.info(
                    f"[TRAIL] Trail SL -> {trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={self._current_atr:.2f})"
                )

        # ── Re-check SL after trail update (FIX-TRAIL-05) ────────────────────
        if is_long and price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER:
            _trail_improved = state.current_sl > risk.sl
            _be_at_entry    = state.be_done and abs(state.current_sl - entry_price) < 1e-6
            if _trail_improved and not _be_at_entry:
                _recheck_reason = f"Trail SL (stage {state.stage})"
            elif _be_at_entry:
                _recheck_reason = "Breakeven SL"
            else:
                _recheck_reason = "Initial SL"
            await self._fire_exit(price, _recheck_reason, source="tick")
            return
        if not is_long and price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER:
            _trail_improved = state.current_sl < risk.sl
            _be_at_entry    = state.be_done and abs(state.current_sl - entry_price) < 1e-6
            if _trail_improved and not _be_at_entry:
                _recheck_reason = f"Trail SL (stage {state.stage})"
            elif _be_at_entry:
                _recheck_reason = "Breakeven SL"
            else:
                _recheck_reason = "Initial SL"
            await self._fire_exit(price, _recheck_reason, source="tick")

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """
        Fire exit once. Idempotent on success.

        FIX-TRAIL-04: up to 3 close_position attempts, no infinite cascade.
        FIX-FILL-PRICE: actual exchange fill used for journal/Telegram P&L.
        FIX-AUDIT-03: source tag forwarded to on_trail_exit callback.
        """
        if self._exit_fired:
            return
        self._exit_fired = True

        logger.info(
            f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f} "
            f"source={source} live_atr={self._current_atr:.2f}"
        )

        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed (ignored): {e}")

        is_long = self._risk.is_long if self._risk else True

        MAX_ATTEMPTS = 3
        success = False
        actual_fill_price: Optional[float] = None
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await self._order_mgr.close_position(
                    is_long=is_long, reason=reason
                )
                success = True
                if isinstance(result, dict):
                    if result.get("info") == "already_closed":
                        logger.info(
                            f"[TRAIL] close_position: position already closed on exchange "
                            f"— treating as exit success (attempt {attempt})"
                        )
                    else:
                        fill = result.get("average") or result.get("price")
                        if fill and float(fill) > 0:
                            actual_fill_price = float(fill)
                        logger.info(
                            f"[TRAIL] close_position: exit order placed on attempt {attempt} "
                            f"fill={actual_fill_price}"
                        )
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[TRAIL] close_position attempt {attempt}/{MAX_ATTEMPTS} failed: {e}"
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)

        if not success:
            logger.error(
                f"[TRAIL] close_position FAILED after {MAX_ATTEMPTS} attempts "
                f"(last error: {last_err}). Marking exit complete to prevent "
                f"infinite retry. ⚠️ MANUAL POSITION CHECK ON DELTA REQUIRED."
            )

        reported_exit_price = actual_fill_price if actual_fill_price is not None else exit_price
        if actual_fill_price is not None and abs(actual_fill_price - exit_price) > 1.0:
            logger.info(
                f"[TRAIL] Exit price corrected: trail_fire={exit_price:.2f} "
                f"actual_fill={actual_fill_price:.2f} "
                f"diff={actual_fill_price - exit_price:+.2f}"
            )

        self._running = False
        if self._on_exit_cb is not None:
            try:
                await self._on_exit_cb(reported_exit_price, reason, source)
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch — safety net only ────────────────────────────────

    async def _get_mark_price(self) -> Optional[float]:
        """
        Fetch current mark price from exchange via REST.
        Backup path only — primary is on_price_tick() from WS feed.
        FIX-AUDIT-01: correct ticker key priority for Delta India.
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

    # ── Feed integration ───────────────────────────────────────────────────────

    def _update_live_atr(self, high: float, low: float) -> None:
        """
        PINE-PARITY-FIX: DISABLED.
        ATR must not change intrabar — only updated at bar close in on_bar_close().
        """
        return

    def push_ws_candle(self, high: float, low: float, source: str = "binance") -> None:
        """
        Called by ws_feed (Delta WS) and binance_price_feed (1m bucket flush).

        FIX-PARITY-01: updates peak_price from live high/low.
        FIX-PARITY-03: schedules immediate exit evaluation for both extremes.
        FIX-DUAL-SOURCE-B: translates Binance high/low to Delta-equivalent space.
        """
        if not self._running or self._exit_fired or self._state is None or self._risk is None:
            return

        is_long = self._risk.is_long

        if source == "binance":
            if self._source_offset is None:
                return
            high = high - self._source_offset
            low  = low  - self._source_offset

        if is_long:
            if high > self._state.peak_price:
                self._state.peak_price = high
        else:
            if self._state.peak_price == 0.0 or low < self._state.peak_price:
                self._state.peak_price = low

        try:
            loop    = asyncio.get_running_loop()
            tp_side = high if is_long else low
            sl_side = low  if is_long else high
            loop.create_task(self._evaluate_tick_pair(tp_side, sl_side))
        except RuntimeError:
            pass
