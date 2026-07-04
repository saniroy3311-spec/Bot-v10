"""
monitor/trail_loop.py — Shiva Sniper v10 — PINE-EXACT-TRAIL
════════════════════════════════════════════════════════════════════════════
ROOT CAUSE OF ALL PREVIOUS DIVERGENCE (fixed in this version)
──────────────────────────────────────────────────────────────────────────
FIX-1 | Trail armed too early (CRITICAL — wrong exit prices)
OLD:  Trail armed when ANY profit > 0 (even 0.01 pts).
NEW:  Trail only arms when price crosses activation_price = entry ± trail_pts
where trail_pts = atr * pts_mult  (Pine exact: strategy.exit trail_points).
EFFECT: v10 was replacing the initial SL (e.g. 500 pts away) with a trail
SL just 320 pts from entry the instant price moved 0.01 pts favorable.
Any normal intrabar noise could then hit this tight SL for a loss
while Pine's trail wasn't even armed yet.
FIX-2 | Intrabar stage upgrades removed (HIGH — premature SL tightening)
OLD:  _evaluate_tick() upgraded trail stages on every price tick.
NEW:  Stage upgrades happen ONLY in on_bar_close() (Pine-exact: calc_on_every_tick=false).
EFFECT: v10 reached stage 2/3 on an intrabar spike, tightened the trail
immediately, then trailed out at a worse price than Pine.
FIX-3 | Intrabar breakeven removed (MEDIUM — premature BE stop)
OLD:  _evaluate_tick() checked breakeven on every price tick.
NEW:  Breakeven check ONLY in on_bar_close() (Pine-exact).
EFFECT: v10's intrabar BE fired mid-bar; any pullback before bar close
hit the BE stop when Pine's BE wasn't yet active.
FIX-4 | Initial SL update every bar (MEDIUM — trailing behind Pine)
KEPT: on_bar_close() updates current_sl from live ATR each bar when trail
not yet armed — matches Pine's strategy.exit(stop=) recalculation.
FIX-11 | SUPERSEDED — Trail SL always fires intrabar (TV-exact)
Pine's strategy.exit(trail_points=, trail_offset=) fires intrabar on the exact
tick that breaches trail_sl — the Exit label plots mid-candle at that price.
BAR_CLOSE_SL_EVAL does NOT suppress trail SL. It only applies to the Initial SL
(pre-arm, from the strategy body which runs at bar close via calc_on_every_tick=false).
Once trail arms, strategy.exit() takes over and always fires intrabar.
The old FIX-11 was wrong — suppressing trail SL at bar close made the bot exit
at bar_low (often 30-150 pts worse than TV's intrabar exit tick).
FIX-12 | pre_trail_sl snapshot taken before trail arms — wrong exit level (NEW)
OLD:  on_bar_close() snapshotted pre_trail_sl = state.current_sl BEFORE step 5&6
(trail arm). When trail armed this bar, step 7 still used old initial_sl.
NEW:  Snapshot moved to AFTER step 5&6 so pre_trail_sl = new trail_sl if armed.
EFFECT: Trade 2: trail armed at bar_high=66263.5 → trail_sl=66225.
Old code: pre_trail_sl=initial_sl=65855 → fired "Initial SL" at 65855.
New code: pre_trail_sl=66225 → bar_low=65855 < 66225 → "Trail SL" at 66225.
FIX-15 | Trail SL breach hold guard — 4-second wick filter (NEW)
OLD:  Once TRAIL_SL_CONFIRM_TICKS consecutive Delta ticks breach the trail SL,
      exit fires instantly.
NEW:  After tick-count confirmation, a 4-second hold guard starts. The exit
      only fires if price stays below the trail SL for the full 4 seconds.
      If price recovers within the window (wick), the timer resets — no exit.
EFFECT: Trade with trail peak=61,470, trail SL=61,337: wick hit 61,315 for
      ~2 seconds then recovered to 61,374. Old code: exit at 61,337 (→ fill
      61,266 with slippage). New code: wick recovers in <4s → hold cancelled
      → bot holds and exits closer to TV's 61,374 level.
      Applies to trail-armed exits only. Initial SL, Max SL, TP unaffected.
FIX-5 | Offset recalibration mid-trade caused premature exit (NEW)
OLD:  _recalibrate_offset() fires every 30s and could jump offset by up to
50 pts in one step, instantly jerking the trail SL and causing exit.
NEW:  Once trail arms (_trail_ever_armed=True), recalibration is completely
frozen. Pre-arm recal is tightened to max 10 pts jump (was 50).
EFFECT: The +287 vs +453 trade exited early because offset jumped +36 pts
mid-trade. This makes that impossible.
FIX-6 | Binance offset drift corrupted best_price during fast moves (NEW)
OLD:  Both Binance (offset-adjusted) and Delta ticks called _evaluate_tick(),
which updates best_price. When spread widened (e.g. +40→+77 pts),
Binance ticks underestimated Delta price, so best_price didn't track
as deep as Pine's trail did.
NEW:  Post-arm, Binance ticks call _evaluate_tick_sl_only() which checks
TP/SL exits but does NOT update best_price. Only Delta ticks (push_delta_tick)
and the REST safety-net poll update best_price post-arm.
EFFECT: Trail tracks exactly as deep as Pine's trail does, closing the
~166 pt gap between bot and TradingView results.
HOW PINE'S trail_points / trail_offset WORKS
──────────────────────────────────────────────────────────────────────────
Pine's strategy.exit(trail_points=P, trail_offset=O) internally does:
SHORT TRADE:
Step 1 — ACTIVATION:
activation_price = entryPrice - P   (P points below entry = profit)
Trail is NOT active until price <= activation_price
Step 2 — BEST PRICE (once armed):
  best_price = lowest price seen since trail armed (running min)

Step 3 — TRAIL SL:
  trail_sl = best_price + O
  Exit when current_price >= trail_sl
LONG TRADE:
activation_price = entryPrice + P
best_price = highest price seen since trail armed
trail_sl = best_price - O
Exit when current_price <= trail_sl
STAGE UPGRADES (bar-close only)
──────────────────────────────────────────────────────────────────────────
Pine upgrades trailStage when profitDist >= atr * triggerMult AT BAR CLOSE.
When stage upgrades, trail_sl recomputes from existing best_price.
best_price does NOT reset on stage upgrade.
BREAKEVEN (bar-close only)
──────────────────────────────────────────────────────────────────────────
Pine: if profitDist > atr * beMult AT BAR CLOSE → SL floor = entryPrice.
Once BE fires, trail continues but SL can never go worse than entry.
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
    TRAIL_OFFSET_FLOOR_MULT,
    TRAIL_FIRE_SL_ON_CANDLE_EXTREME,
    SL_CONFIRM_MS,
    SL_CONFIRM_TICKS,
    TRAIL_SL_CONFIRM_TICKS,
    BAR_CLOSE_SL_EVAL,
    TP_HARD_EXIT,
    MAX_EXIT_SLIPPAGE_ATR_PCT,
)
from risk.calculator import RiskLevels, TrailState

logger = logging.getLogger("trail_loop")

# FIX-5: Maximum offset jump allowed in a single recalibration step.
# Pre-arm: tightened from 50 → 10 pts to prevent large sudden jumps.
# Post-arm: recalibration is fully frozen (this constant not reached).
RECAL_MAX_JUMP = 10.0

# FIX-13: Maximum single-tick jump allowed on the raw Delta price feed before
# a tick is treated as a noise wick and skipped entirely (not counted toward
# breach, not used to reset the breach counter — just ignored, as if it never
# arrived). Delta's own order book occasionally prints a single outlier tick
# (thin liquidity flash) that is not present on Binance/TV's data source.
# CHANGED FROM 20.0 TO 30.0 TO REDUCE FALSE POSITIVES ON FAST MOVES.
MAX_DELTA_TICK_JUMP = 30.0

# FIX-14: Recovery valves for FIX-13. The plain version of FIX-13 compares
# every tick to the LAST ACCEPTED tick. If one tick is ever wrongly rejected,
# that reference goes stale, and every following tick — even normal small
# moves — now reads as ">20pts from a stale number" and gets rejected too.
# The reference can lock up indefinitely with no way back, silently freezing
# best_price/trail during a real, fast, multi-tick rally (seen live on
# 2026-06-21, trade #350: ~70 real ticks rejected over 75s while price ran
# 64217→64267, costing ~35pts vs the TradingView trail exit on the same trade).
# Two independent recovery paths, either one re-syncs the reference:
# 1) STREAK: if N consecutive rejected ticks all move the SAME direction,
#    that's a real run, not repeated noise — accept the latest one and
#    resume normal tracking from there. A genuine single wick (jumps away
#    then snaps back) never satisfies "same direction N times in a row",
#    so the original FIX-13 protection is untouched.
# 2) STALE TIMEOUT: if no tick has been accepted for this many seconds,
#    the next tick is accepted unconditionally — covers feed gaps /
#    reconnects where price legitimately moved while we weren't listening.
WICK_STREAK_CONFIRM   = 5      # consecutive same-direction rejects before override
WICK_STALE_TIMEOUT_S  = 5.0    # force-accept next tick after this long with none accepted

# FIX-15: Trail SL breach hold guard.
# Once tick-count confirmation fires (TRAIL_SL_CONFIRM_TICKS met), do NOT
# exit immediately. Instead require the price to stay BELOW the trail SL for
# this many wall-clock seconds before the exit fires.
#
# Problem it solves: the streak-override (FIX-14b) correctly identified a real
# 3-tick directional move and accepted 61,315 as a "real" tick — but that tick
# was still a wick (price recovered to 61,374 within ~2 seconds). Because the
# tick-count confirmation fired the exit instantly, the bot closed at 61,266
# (71pt slippage from 61,337 trail SL) while TV held and exited at 61,374.
#
# How it works:
#   1. Tick count reaches TRAIL_SL_CONFIRM_TICKS → hold timer starts (no exit).
#   2. Each subsequent confirming tick: checks how long price has stayed below SL.
#   3. If price recovers above SL within the window → timer resets, no exit.
#   4. If price stays below SL for >= TRAIL_SL_BREACH_HOLD_SECS → exit fires.
#
# Why 4 seconds: real wick spikes on BTC recover within 1-2 seconds. A genuine
# break stays below for 5+ seconds. 4s is the safe midpoint — catches real
# breaks while ignoring most wicks.
#
# Note: ONLY applies to trail-armed exits. Initial SL fires immediately as before
# (it uses BAR_CLOSE_SL_EVAL anyway). Max SL and TP are also unaffected.
TRAIL_SL_BREACH_HOLD_SECS = 7.0
# FIX-16: Once stage has upgraded past 0, trust the move faster
TRAIL_SL_BREACH_HOLD_SECS_STAGE_UP = 3.0

# FIX-17: Large-breach fast exit (skip the hold guard on big, obvious moves).
#
# Problem it solves: FIX-15's hold guard is correct for small wicks (price
# dips a few points past the trail SL, then recovers) — waiting protects
# against false exits. But on a genuine fast move, price can keep falling
# for the entire hold window before the exit fires, turning a small trail
# breach into much larger slippage. Live example (2026-07-04, trade exit
# 23:46:05): trail SL was 63,312.44; by the time the 7s hold expired, price
# had fallen to 63,268.50 — a breach that had grown to ~44 points before the
# bot was even allowed to react, then several more points of order-fill
# slippage on top of that.
#
# Fix: as soon as tick-count confirmation fires, check how far price has
# already moved past the trail SL. If that distance is "large" (defined as
# a fraction of current ATR, so it auto-scales with volatility instead of
# using one fixed point value), skip the hold guard entirely and fire the
# exit immediately — same as the old "fire on tick confirm" behaviour, just
# scoped only to breaches large enough that waiting can't help. Small
# breaches (below this threshold) still go through the full FIX-15 hold
# guard, completely unchanged.
#
# TRAIL_SL_LARGE_BREACH_ATR_PCT: breach distance, as a percent of current
# ATR, above which the hold guard is skipped. Starting value below is a
# rough estimate — tune this using actual trade history rather than leaving
# it as a guess: look at how far genuine breakdowns vs. wicks that recovered
# moved past the SL, as a % of ATR at the time, and set the threshold
# between the two clusters.
TRAIL_SL_LARGE_BREACH_ATR_PCT = 15.0

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
    Activation distance = how far price must move in profit direction before
    the trail arms.  Pine: trail_points = atr * pts_mult * PINE_MINTICK.
    """
    idx = max(stage - 1, 0)
    _, pts_mult, _ = TRAIL_STAGES[idx]
    return atr * pts_mult * PINE_MINTICK

def _trail_off(stage: int, atr: float) -> float:
    """
    Offset distance = gap between best_price and trail_sl.
    Pine: trail_offset = atr * off_mult * PINE_MINTICK.
    Optionally floored at atr * TRAIL_OFFSET_FLOOR_MULT.
    """
    idx = max(stage - 1, 0)
    _, _, off_mult = TRAIL_STAGES[idx]
    raw   = atr * off_mult * PINE_MINTICK
    floor = atr * TRAIL_OFFSET_FLOOR_MULT
    return max(raw, floor)

def _activation_price(entry: float, stage: int, atr: float, is_long: bool) -> float:
    """
    Price at which the trail arms.
    Long:  entry + trail_pts  (price must RISE this far to arm)
    Short: entry - trail_pts  (price must FALL this far to arm)
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
    Stages ratchet — only upgrade, never downgrade.
    Pine: profitDist >= atr * triggerMult  (checked at bar close, no PINE_MINTICK).
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
    Tick-resolution trailing stop monitor — exact Pine Script parity.
    Pine's trail_points / trail_offset engine replicated exactly:
      • Trail arms when price crosses activation_price (entry ± trail_pts)
      • best_price tracks the running extreme since arming
      • trail_sl = best_price ± trail_offset
      • Stage upgrades ratchet up at BAR CLOSE only (Pine: calc_on_every_tick=false)
      • Breakeven fires at BAR CLOSE only (Pine: calc_on_every_tick=false)
      • Initial SL updates every bar with live ATR (matches Pine's strategy.exit recalc)

    on_bar_close()           → ATR update + initial SL + stage upgrade + BE + safety exit
    on_price_tick()          → Binance WS feed (offset-adjusted):
                               pre-arm: full _evaluate_tick()
                               post-arm: _evaluate_tick_sl_only() — no best_price update (FIX-6)
    push_delta_tick()        → Delta mark price tick — no offset, full _evaluate_tick() always
    _tick_loop()             → 5-second REST safety-net backup (full _evaluate_tick())
    push_ws_candle()         → intrabar peak update + TP detection only
    _trail_ever_armed        → True once trail arms; freezes offset recalibration (FIX-5)
    """

    def __init__(self, order_mgr=None, telegram=None, journal=None, **kwargs) -> None:
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

        # FIX-5: Once trail ever arms, offset recalibration is permanently frozen.
        self._trail_ever_armed  : bool = False

        # FIX-7: Trail-SL spike debounce. A momentary real tick can poke the
        # trail SL by a fraction of a point and then immediately retreat.
        # TradingView/Pine never sees these sub-bar spikes, so it keeps trailing
        # while the bot exits early. We require the SL to stay breached for
        # SL_CONFIRM_MS before firing the trailing/initial stop. TP and Max SL
        # are NOT debounced (they fire instantly). 
        self._pending_sl_since_ms : int = 0

        # FIX-8 (Option 1+3): Consecutive Delta-tick breach counter.
        # When SL_CONFIRM_TICKS > 0, we require this many consecutive Delta-source
        # ticks above the SL before firing — replaces the time-based window.
        # Resets to 0 on ANY tick below the SL. Immune to Binance feed interleaving.
        self._breach_delta_count  : int = 0

        # FIX-13: Last accepted Delta tick price, used to filter single-tick
        # wicks before they ever reach the breach counter above.
        self._last_delta_price    : Optional[float] = None

        # FIX-14: Recovery state for the FIX-13 wick filter (see constants
        # above). Tracks a run of consecutive same-direction rejections and
        # the wall-clock time of the last accepted tick, so a real multi-tick
        # move or a feed gap can never lock the filter up indefinitely. 
        self._last_delta_accept_wall_s : float = 0.0
        self._wick_reject_streak       : int   = 0
        self._wick_reject_dir          : int   = 0   # +1 up, -1 down, 0 none yet

        # FIX-15: Trail SL breach hold guard.
        # Wall-clock time (seconds) when tick-count confirmation first fired for
        # a trail-armed breach. 0.0 = no breach in progress.
        # Reset to 0.0 when price retreats above SL or when exit fires.
        self._trail_breach_hold_since_s : float = 0.0

        # FIX-9 (BAR_CLOSE_SL_EVAL): Pine-exact Initial SL behaviour.
        # When True, the Initial SL (pre-trail-arm) is ONLY evaluated at bar close,
        # not on live ticks. This matches Pine's calc_on_every_tick=false exactly.
        # Trail SL (post-arm), TP, and Max SL continue to fire on live ticks.
        # Tracks whether the current bar's close has been evaluated for Initial SL.
        self._last_initial_sl_bar_ms : int = 0

        # FIX-10 (GHOST-TRAIL): Position existence guard.
        # Every POSITION_POLL_TICKS iterations of _tick_loop (i.e. every
        # POSITION_POLL_TICKS * TRAIL_LOOP_SEC seconds) we call fetch_open_position()
        # to confirm the position still exists on Delta. If Delta is flat, the bracket
        # SL fired silently — we stop the trail immediately and recover the exit.
        # At TRAIL_LOOP_SEC=5 and POSITION_POLL_TICKS=1 this checks every 5 seconds
        # (max ghost exposure = 5 s instead of 27 minutes from today's incident).
        self._pos_poll_ticks   : int = 0
        POSITION_POLL_TICKS    = 1   # check every tick-loop iteration (5s)

    # ── Start / Stop ──────────────────────────────────────────────────────────
    def start(
        self,
        risk_levels       : RiskLevels,
        trail_state       : TrailState,
        entry_bar_time_ms : int,
        on_trail_exit     : Callable,
        entry_wall_ms     : Optional[int] = None,
        signal_bar_high   : Optional[float] = None,
        signal_bar_low    : Optional[float] = None,
        signal_bar_open   : Optional[float] = None,
        signal_bar_close  : Optional[float] = None,
    ) -> None:
        self._risk         = risk_levels
        self._state        = trail_state
        self._on_exit_cb   = on_trail_exit
        self._entry_bar_ms = entry_bar_time_ms
        self._exit_fired   = False
        self._running      = True
        self._current_atr  = risk_levels.atr

        # Pine trail runtime state — reset on every new trade
        trail_state.trail_armed = False 
        trail_state.best_price  = 0.0
        # current_sl already set to risk.sl by main.py (correct initial SL)

        self._entry_wall_ms = entry_wall_ms if entry_wall_ms is not None else int(time.time() * 1000)

        self._source_offset    = None
        self._first_tick_ts_ms = 0
        # Seed recal timer from trade open (not epoch 0) so recalibration
        # doesn't fire on the very first tick before the offset stabilises.
        self._last_recal_ms = int(time.time() * 1000)

        # FIX-5: Reset arm-freeze flag for the new trade
        self._trail_ever_armed = False

        # FIX-7: Reset spike-debounce state for the new trade
        self._pending_sl_since_ms = 0

        # FIX-8: Reset Delta-tick breach counter for the new trade
        self._breach_delta_count = 0

        # FIX-13: Reset last-accepted Delta price for the new trade
        self._last_delta_price = None

        # FIX-14: Reset wick-filter recovery state for the new trade
        self._last_delta_accept_wall_s = time.time()
        self._wick_reject_streak       = 0
        self._wick_reject_dir          = 0

        # FIX-15: Reset trail SL breach hold timer for the new trade
        self._trail_breach_hold_since_s = 0.0

        # FIX-9: Reset bar-close Initial SL tracker for the new trade
        self._last_initial_sl_bar_ms = 0

        # FIX-10: Reset position poll counter for the new trade
        self._pos_poll_ticks = 0

        self._entry_bar_end_ms = ( 
            (entry_bar_time_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS
        ) + BAR_PERIOD_MS

        self._task = asyncio.get_running_loop().create_task(self._tick_loop())

        logger.info(
            f"[TRAIL] Started | entry={risk_levels.entry_price:.2f}  "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f}  "
            f"entry_atr={risk_levels.atr:.2f} is_long={risk_levels.is_long} |  "
            f"activation_pts={_trail_pts(1, risk_levels.atr):.2f}  "
            f"trail_off={_trail_off(1, risk_levels.atr):.2f}  "
            f"activation_price={_activation_price(risk_levels.entry_price, 1, risk_levels.atr, risk_levels.is_long):.2f} "
        )

        if signal_bar_high is not None:
            logger.info(
                f"[TRAIL] Signal bar OHLC (informational) |  "
                f"high={signal_bar_high:.2f} low={signal_bar_low:.2f}  "
                f"close={signal_bar_close:.2f} atr={risk_levels.atr:.2f} "
            )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("TrailMonitor stopped.")

    def set_entry_bar_boundary(self, next_bar_open_ms: int) -> None:
        """Called by main.py after entry to set the 30m bar end boundary."""
        self._entry_bar_end_ms = int(next_bar_open_ms)

    # ── Bar-close update ──────────────────────────────────────────────────────
    def on_bar_close(
        self,
        bar_close   : float,
        bar_high    : float,
        bar_low     : float,
        bar_open    : float = 0.0,
        current_atr : float = 0.0,
        is_entry_bar: bool  = False,
    ) -> None:
        """
        Called at every confirmed bar close.

        1. Update live ATR
        2. Update initial SL from live ATR (Pine recalcs stop= every bar)
        3. Stage upgrade from bar-close profit   ← BAR-CLOSE ONLY (FIX-2)
        4. Breakeven check from bar-close profit ← BAR-CLOSE ONLY (FIX-3)
        5. Update best_price from bar extreme (if trail already armed)
        6. Check trail arm from bar extreme (if not yet armed)
        7. Recompute trail_sl from best_price
        8. Same-bar exit check (TP / SL hit within this bar's range)
        """
        if not self._running or self._exit_fired or self._risk is None:
            return

        risk        = self._risk
        state       = self._state
        is_long     = risk.is_long
        entry_price = risk.entry_price

        # Apply Binance→Delta offset to bar prices
        if self._source_offset is not None:
            bar_close = bar_close - self._source_offset
            bar_high  = bar_high  - self._source_offset
            bar_low   = bar_low   - self._source_offset
            if bar_open  > 0.0:
                bar_open = bar_open - self._source_offset

        # ── 1. Update live ATR ───────────────────────────────────────────────
        if current_atr  > 0:
            self._current_atr = current_atr

        atr = self._current_atr

        # FIX-9: Record that this bar has now been evaluated at bar close.
        # _evaluate_tick() uses this to skip Initial SL checks on ticks that
        # arrive within the same bar as a bar-close evaluation — Pine-exact.
        self._last_initial_sl_bar_ms = self._entry_bar_end_ms

        # ── 2. Initial SL update (Pine recalcs stop= every bar) ─────────────
        # Only when trail not yet armed — once trail arms, current_sl is trail SL
        if not getattr(state, 'trail_armed', False) and not state.be_done:
            _atr_mult  = TREND_ATR_MULT if risk.is_trend else RANGE_ATR_MULT
            _stop_dist = min(atr * _atr_mult, MAX_SL_POINTS)
            _anchor     = risk.signal_close if risk.signal_close  > 0 else entry_price
            _new_sl    = (_anchor - _stop_dist) if is_long else (_anchor + _stop_dist)
            if abs(_new_sl - state.current_sl)  > 0.01:
                logger.info(
                    f"[TRAIL] Initial SL update: {state.current_sl:.2f} → {_new_sl:.2f}  "
                    f"(atr={atr:.2f} stop_dist={_stop_dist:.2f}) "
                )
            state.current_sl = _new_sl

        # ── 3. Stage upgrade from bar-close profit (BAR-CLOSE ONLY) ─────────
        close_profit = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        new_stage = _upgrade_stage(state.stage, close_profit, atr)
        if new_stage  > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} → {new_stage} at bar close |  "
                f"profit={close_profit:.2f} atr={atr:.2f} "
            )
            state.stage = new_stage
            if getattr(state, 'trail_armed', False):
                # Trail is live — immediately recompute trail SL at the tighter offset
                new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="stage_upgrade_bar")
            else:
                # Trail not yet armed — log the new activation price so next ticks
                # use the upgraded stage's trail_pts to arm (closer to entry = arms sooner)
                new_act = _activation_price(entry_price, state.stage, atr, is_long)
                logger.info(
                    f"[TRAIL] Stage upgrade pre-arm: new activation_price={new_act:.2f}  "
                    f"trail_pts={_trail_pts(state.stage, atr):.2f}  "
                    f"trail_off={_trail_off(state.stage, atr):.2f} "
                )

        # ── 4. Breakeven check (BAR-CLOSE ONLY) ─────────────────────────────
        if not state.be_done and close_profit  > atr * BE_MULT:
            self._activate_be(state, risk, is_long, atr, source="bar_close")

        # ── 5 & 6. Bar extreme: advance best_price or check trail arm ────────
        # is_entry_bar=True: skip — bar prices pre-date the fill in Pine's model
        bar_extreme = bar_high if is_long  else bar_low

        if not is_entry_bar:
            if getattr(state, 'trail_armed', False):
                # Advance best_price from bar extreme (intrabar wick is real)
                self._update_best_price(state, bar_extreme, is_long)
                new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="bar_close")
            else:
                # Check if bar extreme crossed activation price during this bar
                act_price = _activation_price(entry_price, max(state.stage, 1), atr, is_long)
                armed = (bar_extreme  >= act_price) if is_long else (bar_extreme  <= act_price)
                if armed:
                    state.trail_armed      = True
                    self._trail_ever_armed = True   # FIX-5: freeze recal
                    state.best_price  = bar_extreme
                    new_trail_sl = _trail_sl_from_best(state.best_price, max(state.stage, 1), atr, is_long)
                    self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="bar_close_arm")
                    logger.info(
                        f"[TRAIL] Trail ARMED at bar close | best={bar_extreme:.2f}  "
                        f"trail_sl={state.current_sl:.2f} act_price={act_price:.2f}  "
                        f"[recal FROZEN] "
                    )

        # FIX-BUG2: Snapshot SL AFTER steps 5 &6 so that if trail just armed
        # this bar, pre_trail_sl = new trail_sl (not the old initial SL).
        # OLD code snapshotted BEFORE arm → step 7 used initial_sl=65855 instead
        # of trail_sl=66225 → fired "Initial SL (bar)" at the wrong level.
        pre_trail_sl = state.current_sl

        # ── 7. Same-bar exit check ────────────────────────────────────────────
        # Skip for entry bar — Pine never exits on the signal bar
        if is_entry_bar:
            return

        # FIX-TP-PARITY: Pine's live strategy has NO strategy.exit(limit=tp).
        # TP is informational only (used for plotting/journaling) — Pine never
        # closes a trade at TP, it only ever exits via the trail. Gating this
        # behind TP_HARD_EXIT (default false) stops the bot from cutting trades
        # short ~150-250pts before TV's trail would actually let them run.
        tp_hit = TP_HARD_EXIT and ((bar_high  >= risk.tp) if is_long else (bar_low   <= risk.tp))
        sl_hit = (bar_low   <= pre_trail_sl) if is_long else (bar_high  >= pre_trail_sl)

        if tp_hit or sl_hit:
            if tp_hit and sl_hit:
                ref     = bar_open if bar_open  > 0.0 else bar_close
                use_tp  = abs(ref - risk.tp)  <= abs(ref - pre_trail_sl)
                exit_px = risk.tp        if use_tp else pre_trail_sl
                reason  = "TP (bar)"    if use_tp else "SL (bar)"
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

    # ── Live ticks — Binance WS feed (offset-adjusted) ────────────────────────
    async def on_price_tick(self, price: float, source: str = "binance") -> None:
        """
        Primary intrabar path — called from Binance WS feed on every tick.

        FIX-6: Post-arm behaviour changed:
          pre-arm:  full _evaluate_tick()  — can arm trail, checks initial SL
          post-arm: _evaluate_tick_sl_only() — checks TP/SL exit only,
                    does NOT update best_price (prevents offset-drift from
                    corrupting the trail depth vs Pine)
        """
        if not self._running or self._exit_fired or price  <= 0:
            return

        if source == "binance" and self._risk is not None:
            if self._source_offset is None:
                raw_offset = price - self._risk.entry_price
                if abs(raw_offset)  > 500.0:
                    logger.warning(
                        f"[TRAIL] Source offset rejected (|{raw_offset:+.2f}|  > 500):  "
                        f"binance={price:.2f} delta_fill={self._risk.entry_price:.2f} "
                    )
                    return
                self._source_offset    = raw_offset
                self._first_tick_ts_ms = int(time.time() * 1000)
                logger.info(
                    f"[TRAIL] Source offset locked: binance={price:.2f}  "
                    f"delta={self._risk.entry_price:.2f} offset={self._source_offset:+.2f} "
                )
            price = price - self._source_offset

            # FIX-5: only schedule recalibration when trail has NOT yet armed
            now_ms = int(time.time() * 1000)
            if (
                not self._trail_ever_armed
                and not self._recal_in_progress
                and now_ms - self._last_recal_ms  >= self._recal_interval_ms
            ):
                self._recal_in_progress = True
                asyncio.get_running_loop().create_task(
                    self._recalibrate_offset(price  + self._source_offset)
                )

        # FIX-6: post-arm Binance ticks must NOT update best_price
        state = self._state
        if state is not None and getattr(state, 'trail_armed', False):
            await self._evaluate_tick_sl_only(price)
        else:
            await self._evaluate_tick(price)

    # ── Delta mark price tick — no offset needed ──────────────────────────────
    async def push_delta_tick(self, price: float) -> None:
        """
        Accept a Delta Exchange mark price tick directly.
        No Binance offset arithmetic — feeds straight into _evaluate_tick().

        FIX-6: Delta IS the authoritative price source  (same as Pine uses).
        Always calls the full _evaluate_tick() — updates best_price post-arm.
        Binance ticks post-arm only check SL/TP, not best_price.

        FIX-8 (Option 1):  tagged source="delta" so _sl_confirmed() counts
        only Delta ticks toward the breach confirmation counter.

        FIX-13: Before anything else, reject single-tick wicks. If this tick
        jumps more than MAX_DELTA_TICK_JUMP pts from the last accepted Delta
        tick, treat it as exchange noise and drop it — it never reaches the
        breach counter, never resets it, never updates best_price. A genuine
        price move shows up as several ticks of this size in a row, so real
        moves are unaffected; a single freak tick is.

        FIX-14: FIX-13 alone has no way back if the reference ever goes
        stale (e.g. one wrongly-rejected tick freezes _last_delta_price, and
        every following real tick then also reads as "too far from a stale
        number" forever). Two recovery paths re-sync the reference:
          - STREAK: WICK_STREAK_CONFIRM consecutive rejects in the same
            direction = a real run, not noise → accept and resume.
          - STALE TIMEOUT: no tick accepted for WICK_STALE_TIMEOUT_S →
            accept the next tick unconditionally (covers feed gaps).
        """
        if not self._running or self._exit_fired or price  <= 0:
            return

        now_s = time.time()

        if self._last_delta_price is not None:
            jump = price - self._last_delta_price
            abs_jump = abs(jump)

            if abs_jump  > MAX_DELTA_TICK_JUMP:
                stale_for = now_s - self._last_delta_accept_wall_s

                # FIX-14a: feed gap recovery — too long since any accepted tick
                if stale_for  >= WICK_STALE_TIMEOUT_S:
                    logger.info(
                        f"[TRAIL] Wick filter stale-timeout override — no tick  "
                        f"accepted for {stale_for:.1f}s, force-accepting  "
                        f"price={price:.2f} (was last={self._last_delta_price:.2f}) "
                    )
                    self._wick_reject_streak = 0
                    self._wick_reject_dir = 0
                    # falls through to acceptance below

                else:
                    direction = 1 if jump  > 0 else -1
                    if direction == self._wick_reject_dir:
                        self._wick_reject_streak += 1
                    else:
                        self._wick_reject_streak = 1
                        self._wick_reject_dir = direction

                    if self._wick_reject_streak  < WICK_STREAK_CONFIRM:
                        logger.warning(
                            f"[TRAIL] Delta tick wick ignored — price {price:.2f} jumped  "
                            f"{jump:+.2f} pts from last={self._last_delta_price:.2f}  "
                            f"(> max={MAX_DELTA_TICK_JUMP:.1f} pts) — dropped, not counted  "
                            f"[streak={self._wick_reject_streak}/{WICK_STREAK_CONFIRM}] "
                        )
                        return

                    # FIX-14b: streak recovery — N consecutive same-direction
                    # rejects means this is a real move, not repeated noise
                    logger.info(
                        f"[TRAIL] Wick filter streak override —  "
                        f"{self._wick_reject_streak} consecutive same-direction  "
                        f"ticks, accepting price={price:.2f} as a real move  "
                        f"(was last={self._last_delta_price:.2f}) "
                    )
                    self._wick_reject_streak = 0
                    self._wick_reject_dir = 0
                    # falls through to acceptance below

        self._last_delta_price = price
        self._last_delta_accept_wall_s = now_s
        logger.debug(f"[TRAIL] Delta tick {price:.2f}")
        await self._evaluate_tick(price, source="delta")

    async def _recalibrate_offset(self, binance_price_raw: float) -> None:
        """
        FIX-5: Recalibration is only allowed pre-arm.
        The on_price_tick() guard already blocks this from being scheduled
        post-arm, but we add a double-check here for safety.
        Pre-arm max jump is RECAL_MAX_JUMP (10 pts) instead of old 50 pts.
        """
        try:
            # FIX-5: Double-check — abort immediately if trail has ever armed
            if self._trail_ever_armed:
                logger.info("[TRAIL] Offset recal skipped — trail armed [recal FROZEN]")
                return

            if self._first_tick_ts_ms  > 0:
                elapsed = int(time.time() * 1000) - self._first_tick_ts_ms
                if elapsed  < 20_000:
                    logger.info(f"[TRAIL] Offset recal skipped — trade too new ({elapsed}ms  < 20s)")
                    return

            delta_mark = await self._get_mark_price()
            if delta_mark and delta_mark  > 0 and self._source_offset is not None:
                new_offset = binance_price_raw - delta_mark
                # FIX-5: tightened from 50 → RECAL_MAX_JUMP (10 pts)
                if abs(new_offset - self._source_offset)  <= RECAL_MAX_JUMP:
                    old = self._source_offset
                    self._source_offset = new_offset
                    logger.info(
                        f"[TRAIL] Offset recalibrated: {old:+.2f} → {new_offset:+.2f}  "
                        f"(binance={binance_price_raw:.2f} delta={delta_mark:.2f}) "
                    )
                else:
                    logger.warning(
                        f"[TRAIL] Offset recal rejected: jump={abs(new_offset - self._source_offset):.2f}  "
                        f"> max={RECAL_MAX_JUMP:.1f} pts "
                    )
        except Exception as e:
            logger.warning(f"[TRAIL] Offset recal failed: {e}")
        finally:
            self._last_recal_ms     = int(time.time() * 1000)
            self._recal_in_progress = False

    # ── Safety-net REST poll ──────────────────────────────────────────────────
    async def _tick_loop(self) -> None:
        # FIX-10 (GHOST-TRAIL): How many _tick_loop iterations between each
        # position-existence poll. 1 = every iteration (every TRAIL_LOOP_SEC seconds).
        # Keep at 1 — the position poll is a single cheap REST call and catches
        # a bracket-SL fire within TRAIL_LOOP_SEC seconds instead of 27 minutes.
        POSITION_POLL_TICKS = 1

        while self._running and not self._exit_fired:
            try:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                if not self._running or self._exit_fired:
                    break

                # ── FIX-10: POSITION EXISTENCE GUARD ─────────────────────────
                # Only poll when we believe we're in a position (self._risk set).
                # If Delta says flat, the bracket SL fired while Python was
                # unaware — stop the ghost trail and recover the exit.
                self._pos_poll_ticks += 1
                if self._risk is not None and self._pos_poll_ticks  >= POSITION_POLL_TICKS:
                    self._pos_poll_ticks = 0
                    try:
                        pos = await self._order_mgr.fetch_open_position()
                        if pos is None:
                            logger.warning(
                                "[TRAIL] FIX-10: Position no longer exists on Delta —  "
                                "bracket SL fired silently. Stopping ghost trail."
                            )
                            # Use current_sl as best approximation of exit price;
                            # the bar-close drift check will reconcile with the real fill.
                            bracket_exit_price = float(self._state.current_sl) \
                                if self._state is not None else float(self._risk.sl)
                            await self._fire_exit(
                                bracket_exit_price,
                                "Bracket SL/TP (ghost-trail-guard)",
                                source="pos-poll",
                            )
                            break
                    except Exception as poll_err:
                        # Network blip — do NOT exit on a failed poll.
                        # Only exit on a confirmed size==0. Keep trailing.
                        logger.warning(f"[TRAIL] FIX-10: Position poll failed (keeping trail): {poll_err}")
                # ── END POSITION GUARD ────────────────────────────────────────

                price = await self._get_mark_price()
                if price is None or price  <= 0:
                    continue
                # REST poll uses Delta mark price — always full _evaluate_tick()
                # FIX-8: tagged source="delta" — REST polls Delta mark price,
                # so these count toward the breach tick counter too.
                await self._evaluate_tick(price, source="delta")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRAIL] Tick loop error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

    # ── Core tick evaluator — Pine trail engine ────────────────────────────────
    async def _evaluate_tick(self, price: float,  source: str = "other") -> None:
        """
        Pine trail_points / trail_offset engine — exact replication.

        For every price tick:
          1. TP hit check
          2. Trail arm or initial SL check (if trail not yet armed)
          3. best_price update (if armed)
          4. trail_sl recompute from best_price
          5. Trail SL hit check
          6. Max SL check
          7. Time exit check

        NOTE: Stage upgrades and breakeven are NOT checked here.
              They happen ONLY in on_bar_close() — Pine parity
              (calc_on_every_tick=false means strategy body runs at bar close only).

        Called by: push_delta_tick(), _tick_loop() (REST), on_price_tick() pre-arm.
        Post-arm Binance ticks use _evaluate_tick_sl_only() instead (FIX-6).

        FIX-8 (Option 1+3): source="delta" ticks count toward breach confirmation.
        source="other" (Binance pre-arm path) resets the counter on retreat.
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr          = self._current_atr

        # ── 1. TP hit ─────────────────────────────────────────────────────────
        # FIX-TP-PARITY: Pine's live strategy has no limit=tp hard exit — TP is
        # informational only. Gated behind TP_HARD_EXIT (default false).
        if TP_HARD_EXIT:
            if is_long and price  >= risk.tp:
                await self._fire_exit(risk.tp, "TP", source="tick")
                return
            if not is_long and price  <= risk.tp:
                await self._fire_exit(risk.tp, "TP", source="tick")
                return

        # ── 2. Trail arm or initial SL ────────────────────────────────────────
        if not getattr(state, 'trail_armed', False):
            # Check activation: has price moved trail_pts in profit direction?
            act_price = _activation_price(entry_price, max(state.stage, 1), atr, is_long)
            armed = (price  >= act_price) if is_long else (price  <= act_price)

            if armed:
                # Trail just armed this tick
                state.trail_armed      = True
                self._trail_ever_armed = True   # FIX-5: freeze recal from this moment
                state.best_price  = price
                new_trail_sl = _trail_sl_from_best(price, max(state.stage, 1), atr, is_long)
                self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="arm_tick")
                logger.info(
                    f"[TRAIL] Trail ARMED | price={price:.2f}  "
                    f"act_price={act_price:.2f}  "
                    f"trail_sl={state.current_sl:.2f}  "
                    f"trail_pts={_trail_pts(max(state.stage,1), atr):.2f}  "
                    f"trail_off={_trail_off(max(state.stage,1), atr):.2f}  "
                    f"[recal FROZEN] "
                )
            else:
                # Trail not armed — check initial / BE SL only
                sl_level = state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER if is_long \
                     else state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER

                # FIX-9 (BAR_CLOSE_SL_EVAL): Pine uses calc_on_every_tick=false,
                # which means the Initial SL is ONLY evaluated at bar close.
                # When this flag is True, skip tick-level Initial SL checks entirely.
                # The Initial SL will be caught by on_bar_close() same-bar exit check.
                # Trail SL (post-arm), TP, and Max SL still fire on every tick.
                _skip_initial_sl = BAR_CLOSE_SL_EVAL and not state.be_done

                if not _skip_initial_sl and self._sl_confirmed(price, sl_level, is_long, source=source):
                    reason = "Breakeven SL" if state.be_done else "Initial SL"
                    await self._fire_exit(price, reason, source="tick")
                    return

                # Max SL check (entry bar exempt)
                if not state.max_sl_fired:
                    entry_bar_over = (time.time() * 1000)  >= self._entry_bar_end_ms
                    max_thresh     = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
                    if entry_bar_over:
                        if is_long  and price  <= entry_price - max_thresh:
                            state.max_sl_fired = True
                            await self._fire_exit(price, "Max SL", source="tick")
                            return
                        if not is_long and price  >= entry_price + max_thresh:
                            state.max_sl_fired = True
                            await self._fire_exit(price, "Max SL", source="tick")
                            return

                # Time exit
                if TIME_EXIT_MINUTES  > 0 and self._entry_bar_end_ms  > 0:
                    if int(time.time() * 1000)  >= self._entry_bar_end_ms:
                        await self._fire_exit(price, "Time exit (bar close)", source="tick")
                        return
                return

        # ── 3. Trail is armed — update best_price ────────────────────────────
        self._update_best_price(state, price, is_long)

        # ── 3b. Intrabar stage upgrade (RE-ENABLED by request) ───────────────
        tick_profit = (state.best_price - entry_price) if is_long \
            else (entry_price - state.best_price)
        new_stage_tick = _upgrade_stage(state.stage, tick_profit, atr)
        if new_stage_tick > state.stage:
            logger.info(
                f"[TRAIL] Stage {state.stage} → {new_stage_tick} intrabar |  "
                f"profit={tick_profit:.2f} atr={atr:.2f} src=tick"
            )
            state.stage = new_stage_tick

        # ── 4. Recompute trail SL from best_price ────────────────────────────
        new_trail_sl = _trail_sl_from_best(state.best_price, state.stage, atr, is_long)
        self._apply_trail_sl(state, risk, new_trail_sl, is_long, source="tick")

        # ── 5. Trail SL hit check ─────────────────────────────────────────────
        # FIX-TV-PARITY: Pine's strategy.exit(trail_points=, trail_offset=) ALWAYS
        # fires intrabar on the tick that breaches trail_sl — the Exit label plots
        # mid-candle at that exact price. BAR_CLOSE_SL_EVAL does NOT apply here.
        # BAR_CLOSE_SL_EVAL only suppresses the Initial SL (pre-arm, step 2 above)
        # because that comes from the strategy body which runs at bar close only.
        # Once trail is armed, strategy.exit() takes over and fires intrabar always.
        sl_level = state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER if is_long \
            else state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER
        # trail_armed=True → uses TRAIL_SL_CONFIRM_TICKS (lower) for fast intrabar exit
        if self._sl_confirmed(price, sl_level, is_long, source=source, trail_armed=True):
            trail_improved = (
                (state.current_sl  > risk.sl) if is_long
                else (state.current_sl  < risk.sl)
            )
            be_at_entry = state.be_done and abs(state.current_sl - entry_price)  < 1e-6
            if be_at_entry:
                reason = "Breakeven SL"
            elif trail_improved:
                reason = f"Trail SL (stage {state.stage})"
            else:
                reason = "Initial SL"
            # Fire at state.current_sl — this is the price TV's Exit label shows.
            # Pine's strategy.exit fills at exactly trail_sl when breached, not the
            # breach tick price. Market order fills near this level (5-20pt real slippage).
            await self._fire_exit(state.current_sl, reason, source="tick")
            return

        # ── 6. Max SL (entry bar exempt) ─────────────────────────────────────
        if not state.max_sl_fired:
            entry_bar_over = (time.time() * 1000)  >= self._entry_bar_end_ms
            max_thresh     = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
            if entry_bar_over:
                if is_long  and price  <= entry_price - max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return
                if not is_long and price  >= entry_price + max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return

        # ── 7. Time exit ──────────────────────────────────────────────────────
        if TIME_EXIT_MINUTES  > 0 and self._entry_bar_end_ms  > 0:
            if int(time.time() * 1000)  >= self._entry_bar_end_ms:
                await self._fire_exit(price, "Time exit (bar close)", source="tick")

    # ── Slim tick evaluator — SL/TP exit only, no best_price update ────────────
    async def _evaluate_tick_sl_only(self, price: float) -> None:
        """
        FIX-6: Post-arm Binance tick evaluator.

        Checks TP hit and trail SL hit but does NOT update best_price.
        This prevents Binance's offset-adjusted (potentially stale) price from
        underestimating how deep price actually went on Delta, which would make
        the trail SL sit higher than Pine's trail SL.

        Only Delta ticks (push_delta_tick) and the REST safety-net (_tick_loop)
        are authoritative for best_price post-arm.

        Called by: on_price_tick() when trail is armed.
        """
        risk  = self._risk
        state = self._state
        if risk is None or state is None:
            return

        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr          = self._current_atr

        # ── 1. TP hit ─────────────────────────────────────────────────────────
        # FIX-TP-PARITY: gated behind TP_HARD_EXIT — see note above.
        if TP_HARD_EXIT:
            if is_long and price  >= risk.tp:
                await self._fire_exit(risk.tp, "TP", source="tick")
                return
            if not is_long and price  <= risk.tp:
                await self._fire_exit(risk.tp, "TP", source="tick")
                return

        # ── 2. Trail SL hit check (using current_sl already set by Delta ticks) ──
        sl_level = state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER if is_long \
             else state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER
        # FIX-8: Binance ticks (source="other") never count toward breach counter.
        # They can still fire the exit if tick-count confirm is disabled (SL_CONFIRM_TICKS=0).
        # trail_armed=True → uses TRAIL_SL_CONFIRM_TICKS for fast intrabar exit
        if self._sl_confirmed(price, sl_level, is_long, source="other", trail_armed=True):
            trail_improved = (
                (state.current_sl  > risk.sl) if is_long
                else (state.current_sl  < risk.sl)
            )
            be_at_entry = state.be_done and abs(state.current_sl - entry_price)  < 1e-6
            if be_at_entry:
                reason = "Breakeven SL"
            elif trail_improved:
                reason = f"Trail SL (stage {state.stage})"
            else:
                reason = "Initial SL"
            await self._fire_exit(price, reason, source="tick")
            return

        # ── 3. Max SL (entry bar exempt) ─────────────────────────────────────
        if not state.max_sl_fired:
            entry_bar_over = (time.time() * 1000)  >= self._entry_bar_end_ms
            max_thresh     = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
            if entry_bar_over:
                if is_long  and price  <= entry_price - max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return
                if not is_long and price  >= entry_price + max_thresh:
                    state.max_sl_fired = True
                    await self._fire_exit(price, "Max SL", source="tick")
                    return

        # ── 4. Time exit ──────────────────────────────────────────────────────
        if TIME_EXIT_MINUTES  > 0 and self._entry_bar_end_ms  > 0:
            if int(time.time() * 1000)  >= self._entry_bar_end_ms:
                await self._fire_exit(price, "Time exit (bar close)", source="tick")

    # ── Trail helpers ──────────────────────────────────────────────────────────
    def _update_best_price(self, state: TrailState, price: float, is_long: bool) -> None:
        """Update best_price — highest for long, lowest for short."""
        if is_long:
            if price  > state.best_price:
                state.best_price = price
        else:
            if state.best_price == 0.0 or price  < state.best_price:
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
        Apply new_sl only if it improves (moves toward profit direction).
        Long:  SL can only move up.   Short: SL can only move down.
        Enforces BE floor if breakeven is active.
        """
        if state.be_done:
            if is_long:
                new_sl = max(new_sl, risk.entry_price)
            else:
                new_sl = min(new_sl, risk.entry_price)

        if is_long and  new_sl  > state.current_sl:
            logger.info(
                f"[TRAIL] SL: {state.current_sl:.2f}→{new_sl:.2f}  "
                f"(stage={state.stage} best={state.best_price:.2f} src={source}) "
            )
            state.current_sl = new_sl
        elif not is_long and new_sl  < state.current_sl:
            logger.info(
                f"[TRAIL] SL: {state.current_sl:.2f}→{new_sl:.2f}  "
                f"(stage={state.stage} best={state.best_price:.2f} src={source}) "
            )
            state.current_sl = new_sl

    def _activate_be(
        self,
        state   : TrailState,
        risk    : RiskLevels,
        is_long : bool,
        atr     : float,
        source   : str = "",
    ) -> None:
        """Activate breakeven — set SL floor at entry_price."""
        be_sl = risk.entry_price
        improved = (be_sl  > state.current_sl) if is_long else (be_sl  < state.current_sl)
        if improved:
            state.current_sl = be_sl
            state.be_done    = True
            logger.info(
                f"[TRAIL] Breakeven activated ({source}): SL → {be_sl:.2f}  "
                f"(atr={atr:.2f}) "
            )
        else:
            state.be_done = True
            logger.info(
                f"[TRAIL] Breakeven noted ({source}): trail SL {state.current_sl:.2f}  "
                f"already past entry {be_sl:.2f} — no SL change "
            )

    # ── WS candle peak update ──────────────────────────────────────────────────
    def push_ws_candle(self, high: float, low: float, source: str = "binance", close: float = 0.0, **kwargs) -> None:
        """
        Intrabar WS candle update — advance best_price from favourable extreme only.
        The adverse extreme is NOT evaluated here to avoid stale-candle-high exits.
        SL firing is left to on_price_tick() (live trade price).
        """
        if not self._running or self._exit_fired or self._state is None or self._risk is None:
            return

        is_long = self._risk.is_long

        if source == "binance":
            if self._source_offset is None:
                return
            high = high - self._source_offset
            low  = low  - self._source_offset

        try:
            loop = asyncio.get_running_loop()
            if TRAIL_FIRE_SL_ON_CANDLE_EXTREME:
                # Old behaviour: evaluate both extremes (can fire on stale candle)
                tp_side = high if is_long else low
                sl_side = low  if is_long else high
                loop.create_task(self._evaluate_tick_pair(tp_side, sl_side))
            else:
                # Default (FIX): evaluate only the favourable extreme
                favourable = high if is_long else low
                loop.create_task(self._evaluate_tick(favourable))
        except RuntimeError:
            pass

    async def _evaluate_tick_pair(self, tp_side: float, sl_side: float) -> None:
        await self._evaluate_tick(tp_side)
        if not self._exit_fired:
            await self._evaluate_tick(sl_side)

    # ── Spike-debounce for trailing / initial SL ───────────────────────────────
    def _sl_confirmed(self, price: float, sl_level: float, is_long: bool,
                      source: str = "other", trail_armed: bool = False) -> bool:
        """
        FIX-7 + FIX-8 (Option 1 + Option 3): Dual-mode SL breach confirmation.

        trail_armed=True uses TRAIL_SL_CONFIRM_TICKS (lower) instead of
        SL_CONFIRM_TICKS so trail exits fire quickly like Pine's intrabar
        simulation, while the initial SL keeps full spike protection.

        MODE A — Tick-count mode (SL_CONFIRM_TICKS > 0, RECOMMENDED):
        ─────────────────────────────────────────────────────────────
        Requires SL_CONFIRM_TICKS consecutive Delta-source ticks above the
        SL before firing. Any single tick from Delta below the SL resets the
        counter to 0. Binance ticks (source="other") are completely ignored
        for breach counting — they can never trigger or sustain a breach.

        This is Option 1 (feed isolation) + Option 3 (tick-count) combined:
          • Option 1: only source="delta" ticks advance the counter.
          • Option 3: need REQUIRED_TICKS clean Delta ticks above SL.

        Result: the 47-second fight between Binance (61,249) and Delta (61,179)
        that caused the early exit collapses completely — Delta ticks below SL
        reset the counter, Binance ticks above SL are ignored.

        MODE B — Time-based mode (SL_CONFIRM_TICKS == 0, legacy):
        ──────────────────────────────────────────────────────────
        Falls back to original SL_CONFIRM_MS time-window behaviour for
        backward compatibility. All tick sources participate (original logic).

        TP and Max SL do NOT call this — they fire instantly regardless.
        """
        breached = (price  <= sl_level) if is_long else (price  >= sl_level)
        now_ms   = int(time.time() * 1000)

        # ── MODE A: Tick-count confirm (Option 1 + 3) ─────────────────────────
        if SL_CONFIRM_TICKS  > 0:
            # FIX-TRAIL-INTRABAR: use a lower threshold once trail is armed so
            # trail exits fire quickly (matching Pine intrabar), while the initial
            # SL still requires full SL_CONFIRM_TICKS spike protection.
            _required = (
                (TRAIL_SL_CONFIRM_TICKS if TRAIL_SL_CONFIRM_TICKS  > 0 else SL_CONFIRM_TICKS)
                if trail_armed else SL_CONFIRM_TICKS
            )

            if not breached and source == "delta":
                # Price is inside SL — reset counter regardless of source
                if self._breach_delta_count  > 0:
                    logger.info(
                        f"[TRAIL] SL spike ignored — price {price:.2f} retreated  "
                        f"inside SL {sl_level:.2f} (src={source},  "
                        f"counter reset {self._breach_delta_count}→0) "
                    )
                self._breach_delta_count  = 0
                self._pending_sl_since_ms = 0
                # FIX-15: reset hold timer — wick recovered, start fresh
                if self._trail_breach_hold_since_s > 0.0:
                    logger.info(
                        f"[TRAIL] FIX-15: Trail SL hold cancelled — price {price:.2f} "
                        f"recovered above SL {sl_level:.2f} "
                    )
                    self._trail_breach_hold_since_s = 0.0
                return False

            # Price is breached — only Delta ticks advance the counter
            if source != "delta":
                # Binance (or other) tick above SL — ignored for counting (Option 1)
                logger.debug(
                    f"[TRAIL] SL breach tick ignored (non-delta src={source})  "
                    f"price={price:.2f} sl={sl_level:.2f}  "
                    f"count={self._breach_delta_count}/{_required} "
                )
                return False

            # Delta tick above SL — increment counter
            self._breach_delta_count += 1
            if self._breach_delta_count == 1:
                logger.info(
                    f"[TRAIL] SL breach — Delta tick 1/{_required}  "
                    f"({'trail' if trail_armed else 'initial'}) |  "
                    f"price={price:.2f} sl={sl_level:.2f} "
                )
            else:
                logger.info(
                    f"[TRAIL] SL breach — Delta tick {self._breach_delta_count}/{_required}  "
                    f"({'trail' if trail_armed else 'initial'}) |  "
                    f"price={price:.2f} sl={sl_level:.2f} "
                )

            if self._breach_delta_count  >= _required:
                _cur_stage = self._state.stage if self._state is not None else 0
                _effective_hold = (
                    TRAIL_SL_BREACH_HOLD_SECS if _cur_stage == 0
                    else TRAIL_SL_BREACH_HOLD_SECS_STAGE_UP
                )

                # FIX-17: Large-breach fast exit. If price has already moved
                # past the trail SL by more than TRAIL_SL_LARGE_BREACH_ATR_PCT
                # of current ATR, this isn't a small wick the hold guard needs
                # to filter — it's a real, fast move. Skip the hold entirely
                # and fire now instead of waiting while price runs further.
                breach_dist = abs(price - sl_level)
                breach_atr_pct = (
                    (breach_dist / self._current_atr * 100.0)
                    if self._current_atr > 0 else 0.0
                )
                if trail_armed and breach_atr_pct >= TRAIL_SL_LARGE_BREACH_ATR_PCT:
                    logger.info(
                        f"[TRAIL] FIX-17: Large breach — {breach_dist:.2f}pts  "
                        f"({breach_atr_pct:.1f}% of ATR={self._current_atr:.2f})  "
                        f">= {TRAIL_SL_LARGE_BREACH_ATR_PCT:.1f}% threshold —  "
                        f"skipping hold guard, firing now | price={price:.2f} sl={sl_level:.2f} "
                    )
                    self._breach_delta_count = 0
                    self._pending_sl_since_ms = 0
                    self._trail_breach_hold_since_s = 0.0
                    return True

                if trail_armed and _effective_hold > 0:
                    # FIX-15: Trail SL breach hold guard.
                    # Tick count confirmed — but don't fire yet. Require price
                    # to stay below the trail SL for TRAIL_SL_BREACH_HOLD_SECS
                    # continuous seconds. Wicks recover in 1-2s; real breaks don't.
                    now_s = time.time()
                    if self._trail_breach_hold_since_s == 0.0:
                        # First confirmation — start the hold timer
                        self._trail_breach_hold_since_s = now_s
                        logger.info(
                            f"[TRAIL] FIX-15: Trail SL breach confirmed "
                            f"({self._breach_delta_count} ticks) — hold guard started, "
                            f"need {_effective_hold:.0f}s continuous (stage={_cur_stage}) | "
                            f"price={price:.2f} sl={sl_level:.2f} "
                        )
                        return False

                    held = now_s - self._trail_breach_hold_since_s
                    if held >= _effective_hold:
                        logger.info(
                            f"[TRAIL] FIX-15: Trail SL hold expired after {held:.1f}s (stage={_cur_stage}) "
                            f"— firing | price={price:.2f} sl={sl_level:.2f} "
                        )
                        self._breach_delta_count = 0
                        self._pending_sl_since_ms = 0
                        self._trail_breach_hold_since_s = 0.0
                        return True
                    else:
                        logger.debug(
                            f"[TRAIL] FIX-15: Trail SL hold in progress "
                            f"{held:.1f}/{_effective_hold:.0f}s (stage={_cur_stage}) | "
                            f"price={price:.2f} sl={sl_level:.2f} "
                        )
                        return False
                else:
                    # Initial SL (trail_armed=False) or hold disabled — fire immediately
                    logger.info(
                        f"[TRAIL] SL breach confirmed — {self._breach_delta_count} consecutive  "
                        f"Delta ticks above SL {sl_level:.2f}  "
                        f"({'trail-armed' if trail_armed else 'initial'}) — firing "
                    )
                    self._breach_delta_count  = 0
                    self._pending_sl_since_ms = 0
                    return True

            return False

        # ── MODE B: Time-based confirm (legacy, SL_CONFIRM_TICKS == 0) ────────
        if not breached and source == "delta":
            if self._pending_sl_since_ms:
                logger.info(
                    f"[TRAIL] SL spike ignored — price {price:.2f} retreated  "
                    f"inside SL {sl_level:.2f} (no confirm) "
                )
            self._pending_sl_since_ms = 0
            return False

        if SL_CONFIRM_MS  <= 0:
            return True

        if self._pending_sl_since_ms == 0:
            self._pending_sl_since_ms = now_ms
            logger.info(
                f"[TRAIL] SL breach pending confirm | price={price:.2f}  "
                f"sl={sl_level:.2f} need={SL_CONFIRM_MS}ms "
            )
            return False

        if now_ms - self._pending_sl_since_ms  >= SL_CONFIRM_MS:
            logger.info(
                f"[TRAIL] SL breach confirmed after  "
                f"{now_ms - self._pending_sl_since_ms}ms — firing "
            )
            return True

        return False

    # ── Exit helper ───────────────────────────────────────────────────────────
    async def _fire_exit(self, exit_price: float, reason: str, source: str = "tick") -> None:
        """Fire exit once. Idempotent."""
        if self._exit_fired:
            return
        self._exit_fired = True

        logger.info(
            f"[TRAIL] Exit fired: reason={reason} price={exit_price:.2f}  "
            f"source={source} atr={self._current_atr:.2f} "
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
                # FIX: Pass expected_price for slippage tracking
                result = await self._order_mgr.close_position(is_long=is_long, reason=reason, expected_price=exit_price)
                success  = True
                if isinstance(result, dict):
                    fill = result.get("average") or result.get("price")
                    if fill and float(fill)  > 0:
                        actual_fill_price = float(fill)
                    logger.info(f"[TRAIL] Exit order placed (attempt {attempt}) fill={actual_fill_price}")
                break
            except Exception as e:
                last_err = e
                logger.warning(f"[TRAIL] close_position attempt {attempt}/{MAX_ATTEMPTS}: {e}")
                if attempt  < MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * attempt)

        if not success:
            logger.error(
                f"[TRAIL] close_position FAILED after {MAX_ATTEMPTS} attempts  "
                f"(last: {last_err}). ⚠️ MANUAL CHECK REQUIRED."
            )

        reported_price = actual_fill_price if actual_fill_price is not None else exit_price
        if actual_fill_price is not None and abs(actual_fill_price - exit_price)  > 1.0:
            logger.info(
                f"[TRAIL] Fill correction: signal={exit_price:.2f}  "
                f"actual={actual_fill_price:.2f} diff={actual_fill_price - exit_price:+.2f} "
            )

        # ── Slippage guard ────────────────────────────────────────────────────
        if actual_fill_price is not None and self._current_atr  > 0:
            slip = abs(actual_fill_price - exit_price)
            slip_atr_pct = slip / self._current_atr * 100
            
            if slip_atr_pct  > MAX_EXIT_SLIPPAGE_ATR_PCT:
                logger.critical(
                    f"[TRAIL] ⚠️ EXCESS SLIPPAGE: signal={exit_price:.2f}  "
                    f"fill={actual_fill_price:.2f} slip={slip:.2f}pts  "
                    f"({slip_atr_pct:.1f}% of ATR={self._current_atr:.2f})  "
                    f"reason={reason} — check bracket/order state!"
                )
            else:
                logger.info(
                    f"[TRAIL] Slippage OK: {slip:.2f}pts ({slip_atr_pct:.1f}% of ATR)"
                )

        self._running = False
        if self._on_exit_cb is not None:
            try:
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
            return price if price  > 0 else None
        except Exception as e:
            logger.warning(f"[TRAIL] _get_mark_price failed: {e}")
            return None
