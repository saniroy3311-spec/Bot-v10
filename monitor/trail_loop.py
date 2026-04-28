"""
monitor/trail_loop.py — Shiva Sniper v10 — FIX-PARITY-v4
════════════════════════════════════════════════════════════════════════════

NEW FIX IN THIS VERSION (FIX-PARITY-v4):
──────────────────────────────────────────────────────────────────────────
FIX-TRAIL-05 | SL re-check after trail update now uses correct exit reason.
  At the bottom of _evaluate_tick(), after updating trail_sl from the new
  peak, a re-check fires if price already crossed the updated SL level.
  Previously it always labelled the exit "Trail SL (stage X)" — even when
  stage==0 and current_sl is still the original initial SL, or when BE is
  active and current_sl == entry_price.
  Fix: same trail_improved / be_at_entry logic from the main SL-check block
  is now replicated in the re-check, producing correct labels:
    "Initial SL"  — stage 0, SL not improved
    "Breakeven SL" — be_done and current_sl == entry_price
    "Trail SL (stage N)" — trail has improved beyond initial SL
  This matches the reason labels Pine would show and makes journal/Telegram
  exit notifications accurate.

──────────────────────────────────────────────────────────────────────────
FIX-TRAIL-04 | CRITICAL — _fire_exit() now caps close_position retries at 3
  attempts and ALWAYS calls the exit callback at the end, even on full
  failure. This breaks the infinite-error cascade that was visible in
  production logs (10:39:15 onward — bot endlessly retried
  no_position_for_reduce_only).

  ROOT CAUSE OF THE CASCADE:
  Previous _fire_exit() set `self._exit_fired = False` on ANY exception
  from close_position(). That meant:
    1. Trail SL fires → close_position called.
    2. Position is already gone (manual close, exchange auto-close, etc.)
       → ccxt raises ExchangeError("no_position_for_reduce_only").
    3. Exception caught → _exit_fired reset → next tick (≤ 100 ms later)
       fires _evaluate_tick → SL still crossed → another close_position.
    4. Same error → loop forever, ~10 close attempts / second.
  Combined with the missing FIX-OM-003 on the deployed code, the bot
  became completely unable to manage trades or take new entries.

  THE NEW FIX (works WITH FIX-OM-003/005 in orders/manager.py):
    * Up to 3 attempts with linear back-off (0.5 s, 1.0 s).
    * "already closed" return value (FIX-OM-003) is treated as success.
    * After 3 failed attempts: log loudly, mark _running = False, fire
      the exit callback with the best-known exit price. This ensures
      main.py resets in_position, allowing the bot to take new signals
      even if a stuck position required manual cleanup.
    * `self._exit_fired` stays True on permanent failure — no retry storm.

PRESERVED FIXES (all unchanged):
──────────────────────────────────────────────────────────────────────────
FIX-PARITY-01 | CRITICAL — trail calculations now use live_atr (the
  current bar's ATR) instead of frozen entry_atr for ALL trail math.

  Pine Script:
      activePts = atr * trailXPts   ← atr recalculated on EVERY bar
      activeOff = atr * trailXOff   ← same
      if profitDist >= atr * trailXTrigger  ← stage trigger also live

  Old bot: _compute_trail_sl() and _upgrade_stage() both received
  entry_atr (risk.atr, frozen at fill time). Over a multi-bar trade
  where ATR changes from 261 → 340, the bot's trail distances were
  systematically wrong vs Pine — diverging by 50-150 points per stage.

  Fix applied:
  • _upgrade_stage(stage, profit_dist, live_atr)  — renamed param
  • _compute_trail_sl(stage, live_atr, peak, profit, is_long) — live_atr
    used for both activation threshold and offset distance
  • _check_be(profit, live_atr) — renamed, uses current_atr from caller
  • on_bar_close() passes current_atr to all three functions above
  • _evaluate_tick() uses self._current_atr (most recent bar-close ATR)
  • entry_atr (risk.atr) is now ONLY used for initial SL/TP placement
    and Max SL threshold — both are correct as Pine also uses entry-bar
    ATR for these specific calculations.

FIX-PARITY-02 | CRITICAL — WS price push replaces REST polling as the
  primary exit detection path.

  Old: _tick_loop() called fetch_ticker() every 0.1s via REST API.
  Each cycle = ~100-300ms round-trip. Exit sequence = 3 sequential REST
  calls (fetch_ticker + cancel_all_orders + close_position) = 250-500ms
  after price crossed SL/TP. On BTC at 261 ATR, 400ms of price drift
  can move 40-80 points past the exact SL level that Pine filled at.

  Fix: on_price_tick(price) is a new async method called directly by
  ws_feed._process_ws_candle() on every intrabar WS candle update
  (~every 500ms from Delta's feed). _evaluate_tick() runs immediately
  in the same event-loop iteration — zero REST calls before the exit
  decision is made. _tick_loop() is now a 2-second safety fallback that
  only runs if WS candle updates stop arriving.

FIX-PARITY-03 | MEDIUM — push_ws_candle() now triggers an immediate
  exit evaluation for the TP-side and SL-side prices.

  Old: push_ws_candle(h, l) updated state.peak_price but the exit
  check only happened on the next REST poll (up to 100ms later). A
  candle that spiked to TP and reversed within 100ms was invisible.

  Fix: push_ws_candle() schedules _evaluate_tick_pair(tp_px, sl_px)
  immediately after updating peak. The TP-side price is checked first
  (high for longs, low for shorts), then the SL-side. All calls are
  idempotent — if _fire_exit() already ran, subsequent evaluate calls
  return immediately on the _exit_fired guard.

PRESERVED FROM BUG-FIX-AUDIT-v1 (all unchanged):
  FIX-AUDIT-01: _get_mark_price() correct ticker key priority
  FIX-AUDIT-02: asyncio.get_running_loop() throughout
  FIX-AUDIT-03: source tag on _fire_exit() ("bar_close" / "tick")
  FIX-AUDIT-04: Max SL entry-bar block uses candle boundary end
  FIX-EXIT-01: Stage upgrades only in on_bar_close() — tick loop
               reads current stage but never increments it
  FIX-EXIT-04: Bar-open distance for TP-vs-SL priority on same bar
  FIX-EXIT-05: BE activation only at bar close
  FIX-EXIT-06: BAR_PERIOD_MS computed from CANDLE_TIMEFRAME
  FIX-EXIT-07: Stage-0 trail uses stage-1 params (idx = max(stage-1,0))
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

def _upgrade_stage(current_stage: int, profit_dist: float, live_atr: float) -> int:
    """
    Returns the highest trail stage unlocked by profit_dist.
    Stages only upgrade, never downgrade — matches Pine's `var trailStage`.

    FIX-PARITY-01: parameter renamed to live_atr.
      Pine: if profitDist >= atr * trailXTrigger   ← atr is current bar ATR
      Old bot passed entry_atr (frozen). Now receives current_atr from
      on_bar_close() so trigger thresholds scale with live volatility.

    IMPORTANT: Only call from on_bar_close() with bar-close profit_dist.
    (FIX-EXIT-01 — never call from tick loop.)
    """
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
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

    FIX-PARITY-01: replaces entry_atr with live_atr.
      Pine: activePts = atr * trailXPts  (current bar ATR, not entry ATR)
            activeOff = atr * trailXOff
      Old bot froze entry_atr at fill, causing trail distances to diverge
      from Pine whenever ATR moved during the trade. Now uses live_atr
      (updated every bar close via on_bar_close / self._current_atr).

    FIX-EXIT-07 (preserved): stage==0 uses stage-1 params (idx clamp).
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]

    activation = live_atr * pts_mult   # FIX-PARITY-01: was entry_atr
    offset     = live_atr * off_mult   # FIX-PARITY-01: was entry_atr

    if peak_profit_dist < activation:
        return None

    return (peak_price - offset) if is_long else (peak_price + offset)


def _check_be(current_profit: float, live_atr: float) -> bool:
    """
    Returns True if breakeven should activate.

    FIX-PARITY-01: parameter renamed to live_atr to match Pine.
      Pine: beTrigger = atr * beMult   ← current bar ATR
      Caller (on_bar_close) now passes current_atr instead of entry_atr.
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

        self._current_atr     : float = 0.0   # FIX-PARITY-01: live ATR updated each bar

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
        self._current_atr  = risk_levels.atr   # seed with entry-bar ATR; updated each bar close

        # FIX-AUDIT-04: compute the end of the current candle boundary.
        self._entry_bar_end_ms = (
            (entry_bar_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        ) + BAR_PERIOD_MS

        # FIX-AUDIT-02: get_running_loop() — always valid inside asyncio.run()
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
          1. Update live ATR for all subsequent trail calculations (FIX-PARITY-01)
          2. Upgrade trail stage using bar-CLOSE profit (FIX-EXIT-01)
          3. Check Breakeven activation from bar-close profit (FIX-EXIT-05)
          4. Update peak_price with bar extreme (high/low)
          5. Recompute trail SL from bar extreme using live ATR (FIX-PARITY-01)
          6. Same-bar exit check (TP and/or SL hit this bar)
          7. Resolve TP-vs-SL priority via bar_open distance (FIX-EXIT-04)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk  = self._risk
        state = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price

        # ── 1. Update live ATR (FIX-PARITY-01) ──────────────────────────────
        # All trail distance calculations below use current_atr, not entry_atr.
        # entry_atr (risk.atr) is only used for initial SL/TP and Max SL.
        self._current_atr = current_atr

        # ── 2. Upgrade trail stage from bar CLOSE profit (FIX-EXIT-01) ──────
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, current_atr)  # FIX-PARITY-01
        if new_stage > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} -> {new_stage} | "
                f"bar_close_profit={close_profit:.2f} live_atr={current_atr:.2f}"
            )
            state.stage = new_stage

        # ── 3. Breakeven check from bar CLOSE profit (FIX-EXIT-05) ──────────
        if not state.be_done and _check_be(close_profit, current_atr):  # FIX-PARITY-01
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
        # FIX-PARITY-01: use current_atr (not entry_atr) for trail distance.
        if is_long:
            bar_peak_profit = bar_high - entry_price
            _bar_trail_sl = _compute_trail_sl(
                state.stage, current_atr, bar_high, bar_peak_profit, True
            )
            if _bar_trail_sl is not None and _bar_trail_sl > state.current_sl:
                state.current_sl = _bar_trail_sl
                logger.debug(
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
                logger.debug(
                    f"[TRAIL] Bar-close trail SL -> {_bar_trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={current_atr:.2f})"
                )

        # ── 6. Same-bar exit check ────────────────────────────────────────────
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
            # FIX-AUDIT-02: get_running_loop()
            # FIX-AUDIT-03: source="bar_close"
            asyncio.get_running_loop().create_task(
                self._fire_exit(exit_px, reason, source="bar_close")
            )

    # ── WS price push — primary exit detection (FIX-PARITY-02) ──────────────

    async def on_price_tick(self, price: float) -> None:
        """
        FIX-PARITY-02: Primary intrabar exit detection path.

        Called by ws_feed._process_ws_candle() on every intrabar WS candle
        update with the candle's close (last trade price). Replaces REST
        polling as the first responder — zero REST calls before the exit
        decision is made, matching Pine's tick-level execution model.

        _tick_loop() continues as a 2-second safety net for cases where
        WS updates stall.
        """
        if not self._running or self._exit_fired or price <= 0:
            return
        await self._evaluate_tick(price)

    async def _evaluate_tick_pair(self, tp_side: float, sl_side: float) -> None:
        """
        FIX-PARITY-03: Evaluate TP-side price first, then SL-side.
        Scheduled by push_ws_candle() after updating peak_price.
        """
        await self._evaluate_tick(tp_side)
        if not self._exit_fired:
            await self._evaluate_tick(sl_side)

    # ── Internal tick loop — safety net only (FIX-PARITY-02) ─────────────────

    async def _tick_loop(self) -> None:
        """
        FIX-PARITY-02: Demoted to 2-second safety-net REST poll.

        Primary exit detection is now via on_price_tick() pushed from the
        WS feed. This loop only runs if WS updates stop arriving, ensuring
        SL/TP are checked at least every TRAIL_LOOP_SEC seconds regardless.

        Stage upgrades and BE activation are NOT done here (FIX-EXIT-01/05).
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

        FIX-PARITY-01: trail SL now computed with self._current_atr
          (most recent bar-close ATR) instead of frozen entry_atr.
          self._current_atr is seeded at entry and updated each bar close
          in on_bar_close(). Between bar closes it holds the last known
          bar-close ATR — matching Pine's behaviour (Pine uses the most
          recent bar's atr, which is also bar-close ATR since
          calc_on_every_tick=false).

        FIX-EXIT-01: NO stage upgrade here — only in on_bar_close().
        FIX-EXIT-05: NO BE activation here — only in on_bar_close().

        Priority order (matches Pine Script evaluation order):
          1. TP limit
          2. Hard SL / BE SL / Trail SL (current_sl)
          3. Max SL dynamic (uses live ATR per FIX-EXIT-03)
          4. Trail SL update from intrabar peak using self._current_atr
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        entry_price = risk.entry_price

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
        if not state.max_sl_fired:
            # FIX-EXIT-03: Max SL uses live ATR (self._current_atr), not entry_atr.
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
        # FIX-PARITY-01: self._current_atr is the most recent bar-close ATR.
        # Matches Pine's behaviour: atr is not re-tick-sampled intrabar, it
        # holds the last bar-close value between bar closes.
        trail_sl = _compute_trail_sl(
            stage            = state.stage,
            live_atr         = self._current_atr,   # FIX-PARITY-01: was entry_atr
            peak_price       = state.peak_price,
            peak_profit_dist = peak_profit,
            is_long          = is_long,
        )
        if trail_sl is not None:
            if is_long and trail_sl > state.current_sl:
                state.current_sl = trail_sl
                logger.debug(
                    f"[TRAIL] Trail SL -> {trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={self._current_atr:.2f})"
                )
            elif not is_long and trail_sl < state.current_sl:
                state.current_sl = trail_sl
                logger.debug(
                    f"[TRAIL] Trail SL -> {trail_sl:.2f} "
                    f"(stage {state.stage}, live_atr={self._current_atr:.2f})"
                )

        # Re-check SL after trail update (in case trail just moved past current price)
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
            return

    # ── Exit helper ───────────────────────────────────────────────────────────

    async def _fire_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """
        Fire exit once. Idempotent on success.

        FIX-AUDIT-03: `source` parameter identifies the detection path:
          "bar_close" → same-bar detection in on_bar_close()
          "tick"      → intrabar from on_price_tick() or _tick_loop()
        source is forwarded to on_trail_exit callback so main.py can
        decide whether to consume _pending_signal.

        FIX-TRAIL-04 (NEW — replaces the dangerous "reset _exit_fired on
        any failure" pattern that produced the 10:39 cascade in production):

        • Up to 3 close_position attempts with 0.5s, 1.0s back-off.
        • {"info": "already_closed"} from FIX-OM-003/005 is treated as
          success — position is gone, that's the outcome we wanted.
        • After 3 failures: log loudly, leave _exit_fired=True (no retry
          storm), STILL fire the exit callback so main.py resets state
          and the bot can take new entries. Manual cleanup may be needed
          on the exchange side, but the bot will not be locked up.
        """
        if self._exit_fired:
            return
        self._exit_fired = True

        logger.info(
            f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f} "
            f"source={source} live_atr={self._current_atr:.2f}"
        )

        # Best-effort cancel of any leftover orders. Never raises (FIX-OM-005).
        try:
            await self._order_mgr.cancel_all_orders()
        except Exception as e:
            logger.warning(f"[TRAIL] cancel_all_orders failed (ignored): {e}")

        is_long = self._risk.is_long if self._risk else True

        # ── FIX-TRAIL-04: bounded retries, no infinite cascade ───────────────
        MAX_ATTEMPTS = 3
        success = False
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = await self._order_mgr.close_position(
                    is_long=is_long, reason=reason
                )
                success = True
                if isinstance(result, dict) and result.get("info") == "already_closed":
                    logger.info(
                        f"[TRAIL] close_position: position already closed on exchange "
                        f"— treating as exit success (attempt {attempt})"
                    )
                else:
                    logger.info(
                        f"[TRAIL] close_position: exit order placed on attempt {attempt}"
                    )
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[TRAIL] close_position attempt {attempt}/{MAX_ATTEMPTS} "
                    f"failed: {e}"
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)

        if not success:
            # PERMANENT failure — do NOT keep retrying.
            # _exit_fired stays True (no retry storm). Mark not running.
            # Fire the callback anyway so main.py resets in_position and
            # the bot stays responsive to new bar-close signals. The user
            # may need to manually flatten any residual position; the bot
            # will not be able to manage what it cannot close.
            logger.error(
                f"[TRAIL] close_position FAILED after {MAX_ATTEMPTS} attempts "
                f"(last error: {last_err}). Marking exit complete to prevent "
                f"infinite retry. ⚠️ MANUAL POSITION CHECK ON DELTA REQUIRED."
            )

        self._running = False
        if self._on_exit_cb is not None:
            try:
                await self._on_exit_cb(exit_price, reason, source)
            except Exception as e:
                logger.error(f"[TRAIL] exit callback error: {e}", exc_info=True)

    # ── Exchange price fetch — safety net only (FIX-PARITY-02) ──────────────

    async def _get_mark_price(self) -> Optional[float]:
        """
        Fetch current mark price from exchange via REST.

        FIX-PARITY-02: This is now a BACKUP path only (called from
        _tick_loop every 2s). Primary price feed is on_price_tick()
        from the WS candle stream.

        FIX-AUDIT-01: Correct ticker key priority for Delta India:
          1. ticker["markPrice"]             — ccxt-normalised
          2. ticker["info"]["mark_price"]    — raw Delta field
          3. ticker["last"]                  — last traded price
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

    def push_ws_candle(self, high: float, low: float) -> None:
        """
        Called by ws_feed on every intrabar WS candle update.

        FIX-PARITY-01 (preserved): Updates peak_price from live high/low
          so trail SL tightens as the bar develops.

        FIX-PARITY-03 (new): After updating peak, schedules an immediate
          exit evaluation for both the TP-side and SL-side prices. This
          closes the gap where a candle spike hit TP and reversed before
          the next REST poll — the bot now sees it within one event-loop
          iteration of the WS candle arriving.

          TP-side: high for longs (TP is above entry), low for shorts.
          SL-side: low for longs (SL is below entry), high for shorts.
        """
        if not self._running or self._exit_fired or self._state is None or self._risk is None:
            return

        is_long = self._risk.is_long

        # Update intrabar peak (unchanged from original)
        if is_long:
            if high > self._state.peak_price:
                self._state.peak_price = high
        else:
            if self._state.peak_price == 0.0 or low < self._state.peak_price:
                self._state.peak_price = low

        # FIX-PARITY-03: schedule exit evaluation for both extremes
        # TP-side first (high for long, low for short), then SL-side.
        try:
            loop    = asyncio.get_running_loop()
            tp_side = high if is_long else low
            sl_side = low  if is_long else high
            loop.create_task(self._evaluate_tick_pair(tp_side, sl_side))
        except RuntimeError:
            pass  # no running loop — called from test or non-async context
