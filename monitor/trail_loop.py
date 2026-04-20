"""
monitor/trail_loop.py — Shiva Sniper v6.5 (PINE-EXACT-v13)

═══════════════════════════════════════════════════════════════════════
THREE CRITICAL BUGS FIXED IN THIS VERSION (vs Bot-v10 on Hostinger)
═══════════════════════════════════════════════════════════════════════

BUG-1 (PRIMARY) — ENTRY_GUARD_MS = 5000 blocks same-candle exits
─────────────────────────────────────────────────────────────────────
ROOT CAUSE:
  ENTRY_GUARD_MS = 5000 imposed a 5-second hard window after entry
  during which ALL exits (SL, TP, trail) were suppressed. _in_entry_guard()
  returned True for 5 seconds and the TP / Max-SL / SL checks all had
  `if not self._in_entry_guard(now_ms):` guards wrapping them.

  Pine Script's strategy.exit() is active the EXACT millisecond the
  entry order is confirmed. There is no grace period whatsoever.

SYMPTOM:
  Bot enters → market spikes → Pine hits trail/SL/TP within first 5s
  of the candle → Pine closes → bot artificially holds the position,
  running a diverged calculation for the rest of the candle.

FIX:
  ENTRY_GUARD_MS set to 0. _in_entry_guard() now always returns False.
  TP, Max-SL, and SL checks fire from the very first tick. Pine parity.

─────────────────────────────────────────────────────────────────────
BUG-2 (SECONDARY) — _bar_just_closed skips first-tick exit evaluation
─────────────────────────────────────────────────────────────────────
ROOT CAUSE:
  After on_bar_close() ran, it set self._bar_just_closed = True.
  The very next tick in _on_tick() would hit that guard, skip ALL
  exit evaluation (returning early after only updating peak), and
  reset the flag. This suppressed exits on the first poll cycle
  of every new bar — the exact moment same-candle exits are most
  likely to fire (e.g., a gap open that immediately hits SL/TP).

  Pine has no equivalent suppression. strategy.exit() evaluates on
  every tick including the first tick of each new bar.

FIX:
  Entire `if self._bar_just_closed:` block removed from _on_tick().
  Peak tracking in that block is redundant — step 1 of _on_tick()
  already updates state.peak_price on every tick.
  The `_bar_just_closed` attribute is retained in __init__ / start()
  for backward compatibility but is no longer used.

─────────────────────────────────────────────────────────────────────
BUG-3 (PRIMARY) — FIX-TRAIL-13 froze TP at entry (WRONG diagnosis)
─────────────────────────────────────────────────────────────────────
ROOT CAUSE (BUG-TP-FROZEN-001):
  FIX-TRAIL-13 claimed "Pine's strategy.exit(limit=longTP) is set ONCE
  at entry and never changes." This is FALSE.

  In Pine Script these lines are OUTSIDE any `if` block and therefore
  execute on EVERY bar close, including every bar after entry:

      stopDist = math.min(atr * atrMult, maxSLPoints)   ← current ATR
      longSL   = entryPrice - stopDist
      longTP   = entryPrice + stopDist * rr
      strategy.exit("Exit TL", stop=longSL, limit=longTP,
                    trail_points=activePts, trail_offset=activeOff)

  Each call REPLACES the existing exit order with freshly computed SL
  and TP. Both float with ATR each bar; only entryPrice is anchored.

SYMPTOM:
  ATR shrinks after entry → Pine's TP moves CLOSER to entry.
  Price reaches Pine's new (closer) TP → Pine exits.
  Bot's TP was frozen at original (far) value → bot holds with a
  "totally different" TP, sitting in a position Pine already closed.

  ATR grows after entry → opposite: bot's frozen TP is hit first,
  causing premature exit relative to Pine.

FIX (FIX-TRAIL-14):
  Remove frozen_tp. Recalculate new_tp alongside new_sl every bar
  close using current ATR, exactly matching Pine's per-bar
  strategy.exit() call. Both new_sl and new_tp use:
      new_stop_dist = min(current_atr * atr_mult, MAX_SL_POINTS)
  The trail-SL ratchet (state.current_sl) is unchanged — it only
  tightens via the tick loop's max/min ratchet guard.

═══════════════════════════════════════════════════════════════════════
PRESERVED FIXES (unchanged from v10/v11)
═══════════════════════════════════════════════════════════════════════
FIX-TRAIL-12 | trail_points-only SL formula (_compute_trail_sl)
FIX-TRAIL-10 | Never widen state.current_sl when ATR grows between bars
FIX-TRAIL-9  | Removed peak_profit_dist gate from _compute_trail_sl
FIX-TRAIL-8  | Preserve intra-bar peak extremes at bar boundary
FIX-TRAIL-7  | Stage upgrades only at bar close (OPTION-B-1 removed)
FIX-TRAIL-6  | (guard removed but attribute kept for API compat)
FIX-TRAIL-5  | Cancel/restart tick task at bar boundary
OPTION-A-1   | Poll every TRAIL_LOOP_SEC seconds
OPTION-A-2   | Persistent exchange connection (no reconnect per bar)
FIX-TRAIL-1..4 | Prior peak-reset and race-condition fixes
═══════════════════════════════════════════════════════════════════════
"""

import asyncio
import logging
import time
from typing import Optional, Callable

import ccxt.async_support as ccxt

from risk.calculator import (
    RiskLevels, TrailState, calc_real_pl,
    max_sl_hit,
)
from config import (
    SYMBOL, DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    ALERT_QTY, TRAIL_SL_PRE_FIRE_BUFFER, TRAIL_LOOP_SEC,
    TRAIL_STAGES, BE_MULT,
    TREND_RR, RANGE_RR, TREND_ATR_MULT, RANGE_ATR_MULT, MAX_SL_POINTS,
)

logger = logging.getLogger(__name__)

# BUG-1 FIX: Was 5 * 1000 (5 seconds). Set to 0 — exits fire immediately,
# matching Pine Script's strategy.exit() which is active from the first
# millisecond after entry confirmation. No grace period.
ENTRY_GUARD_MS = 0


# ─── Stage helpers ────────────────────────────────────────────────────────────

def _get_active_params(stage: int, atr: float):
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def _calc_new_stage(profit_dist: float, atr: float) -> int:
    """
    Highest stage satisfied by profit_dist.
    Called ONLY at bar close (on_bar_close()) — Pine parity.
    Pine runs calc_on_every_tick=false so stage logic executes once per bar.
    """
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if profit_dist >= atr * trigger_mult:
            return i + 1
    return 0


def _compute_trail_sl(
    stage: int,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
    atr: float,
) -> Optional[float]:
    """
    Pine-exact trail stop: SL = peak ± trail_POINTS only.

    FIX-TRAIL-12A: Use active_pts_val only (not pts + off combined).

    Pine Script strategy.exit(trail_points=X, trail_offset=Y):
      trail_points = distance of the trailing STOP below/above the peak.
      trail_offset = limit-order execution slippage allowance ONLY.
                     Does NOT move the SL level.

    Stage SL distances from peak:
      Stage 0/1 : peak ± 0.70 ATR
      Stage 2   : peak ± 0.55 ATR
      Stage 3   : peak ± 0.45 ATR
      Stage 4   : peak ± 0.30 ATR
      Stage 5   : peak ± 0.20 ATR

    No gate on peak_profit_dist. Pine applies trail from tick 1.
    The max/min ratchet in _on_tick() ensures the trail never widens.
    """
    _, active_pts, _ = TRAIL_STAGES[max(stage - 1, 0)]
    active_pts_val = atr * active_pts
    return (peak_price - active_pts_val) if is_long else (peak_price + active_pts_val)


# ─── TrailMonitor ─────────────────────────────────────────────────────────────

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal

        self.risk:  Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None

        self._running        = False
        self._task: Optional[asyncio.Task] = None
        self._exchange: Optional[ccxt.delta] = None   # OPTION-A-2: persistent
        self._exit_triggered = False

        self.on_trail_exit: Optional[Callable] = None
        self.entry_bar_time_ms: Optional[int] = None

        self._active_pts: float = 0.0
        self._active_off: float = 0.0
        # BUG-2 FIX: _bar_just_closed attribute kept for API/recovery compat
        # but the guard block in _on_tick() has been fully removed.
        self._bar_just_closed: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels: RiskLevels,
        trail_state: TrailState,
        entry_bar_time_ms: Optional[int] = None,
        on_trail_exit: Optional[Callable] = None,
    ):
        self.risk            = risk_levels
        self.state           = trail_state
        self.on_trail_exit   = on_trail_exit
        self._exit_triggered = False
        self._bar_just_closed = False
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)

        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        self._active_pts, self._active_off = _get_active_params(0, risk_levels.atr)
        self._running = True

        # OPTION-A-2: Build exchange connection ONCE here
        _base_url = (
            "https://testnet-api.india.delta.exchange"
            if DELTA_TESTNET
            else "https://api.india.delta.exchange"
        )
        self._exchange = ccxt.delta({
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls"           : {"api": {"public": _base_url, "private": _base_url}},
        })

        self._task = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long} "
            f"poll_interval={TRAIL_LOOP_SEC}s [OPTION-A-1] "
            f"[ENTRY_GUARD_MS={ENTRY_GUARD_MS} — exits fire immediately]"
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self._exchange:
            asyncio.create_task(self._close_exchange())

    async def _close_exchange(self):
        try:
            if self._exchange:
                await self._exchange.close()
                self._exchange = None
        except Exception as e:
            logger.debug(f"Exchange close: {e}")

    def on_bar_close(
        self,
        bar_close: float,
        bar_high: float,
        bar_low: float,
        current_atr: float,
    ):
        if not self._running or self.risk is None or self.state is None:
            return

        # FIX-TRAIL-5: Cancel tick task — restart after full state update
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

        # BUG-2 FIX: _bar_just_closed is set but the guard that used it has
        # been removed from _on_tick(). Setting it here is harmless / compat.
        self._bar_just_closed = True

        # BUG-3 FIX (FIX-TRAIL-14): Recalculate BOTH SL and TP every bar
        # with current ATR — matching Pine's per-bar strategy.exit() call.
        #
        # Pine Script (outside any `if` block, runs every bar close):
        #     stopDist = math.min(atr * atrMult, maxSLPoints)   ← current ATR
        #     longSL   = entryPrice - stopDist
        #     longTP   = entryPrice + stopDist * rr
        #     strategy.exit("Exit TL", stop=longSL, limit=longTP, ...)
        #
        # Both longSL and longTP float with ATR; only entryPrice is anchored.
        # Freezing TP (FIX-TRAIL-13) was a misdiagnosis — removed here.
        old_atr       = self.risk.atr
        old_sl        = self.risk.sl
        atr_mult      = TREND_ATR_MULT if self.risk.is_trend else RANGE_ATR_MULT
        rr            = TREND_RR       if self.risk.is_trend else RANGE_RR
        new_stop_dist = min(current_atr * atr_mult, MAX_SL_POINTS)
        entry_price_  = self.risk.entry_price
        is_long_      = self.risk.is_long

        # Pine: SL and TP both use the SAME new_stop_dist (current ATR)
        if is_long_:
            new_sl = entry_price_ - new_stop_dist
            new_tp = entry_price_ + new_stop_dist * rr
        else:
            new_sl = entry_price_ + new_stop_dist
            new_tp = entry_price_ - new_stop_dist * rr

        self.risk = RiskLevels(
            entry_price = entry_price_,
            sl          = new_sl,
            tp          = new_tp,    # FIX-TRAIL-14: recalculated every bar (Pine parity)
            stop_dist   = new_stop_dist,
            atr         = current_atr,
            is_long     = is_long_,
            is_trend    = self.risk.is_trend,
        )

        trail_has_moved_sl = (
            (is_long_  and self.state.current_sl > old_sl + 1.0) or
            (not is_long_ and self.state.current_sl < old_sl - 1.0)
        )
        if not trail_has_moved_sl:
            # FIX-TRAIL-10: Never widen state.current_sl when ATR increases.
            # Pine DOES update its stop= every bar (risk.sl correctly reflects
            # Pine's formula). But state.current_sl is the ratcheted effective
            # stop — it should only improve (tighten), never widen.
            # Once the first tick fires, trail_sl is already tighter than the
            # initial SL and trail_has_moved_sl = True, so this guard is a no-op
            # in steady state; it only protects the pre-first-tick window.
            if is_long_:
                self.state.current_sl = max(new_sl, self.state.current_sl)
            else:
                self.state.current_sl = min(new_sl, self.state.current_sl)

        logger.info(
            f"[BAR CLOSE] ATR {old_atr:.2f}→{current_atr:.2f} | "
            f"stop_dist={new_stop_dist:.2f} | "
            f"sl={new_sl:.2f} tp={new_tp:.2f}(pine-recalc) | "
            f"current_sl={'trail=' + str(round(self.state.current_sl,2)) if trail_has_moved_sl else str(round(new_sl,2))}"
        )

        risk, state = self.risk, self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        close_profit_dist = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        close_profit_dist = max(0.0, close_profit_dist)

        new_stage = max(state.stage, _calc_new_stage(close_profit_dist, atr))
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            self._active_pts, self._active_off = _get_active_params(new_stage, atr)
            logger.info(
                f"[BAR CLOSE] Trail stage {old_stage}→{new_stage} "
                f"| close={bar_close:.2f} profit_dist={close_profit_dist:.2f} "
                f"active_off={self._active_off:.2f}"
            )
        else:
            self._active_pts, self._active_off = _get_active_params(state.stage, atr)

        if not state.be_done and close_profit_dist > atr * BE_MULT:
            state.be_done = True
            if (is_long and entry_price > state.current_sl) or \
               (not is_long and entry_price < state.current_sl):
                state.current_sl = entry_price
                logger.info(
                    f"[BAR CLOSE] Breakeven SL set to entry={entry_price:.2f} "
                    f"| close_profit_dist={close_profit_dist:.2f}"
                )

        # FIX-TRAIL-8: Preserve intra-bar peak extremes.
        # Long:  keep highest of current peak, bar_high, bar_close
        # Short: keep lowest  of current peak, bar_low,  bar_close
        if is_long:
            state.peak_price = max(state.peak_price, bar_high, bar_close)
        else:
            state.peak_price = min(state.peak_price, bar_low, bar_close)

        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak={state.peak_price:.2f} "
            f"(bar_high={bar_high:.2f} bar_low={bar_low:.2f} bar_close={bar_close:.2f}) "
            f"current_sl={state.current_sl:.2f} be_done={state.be_done} atr={atr:.2f}"
        )

        # FIX-TRAIL-5 + OPTION-A-2: Restart task coroutine, NOT the exchange
        if self._running:
            self._task = asyncio.create_task(self._run())
            logger.debug("[BAR CLOSE] Tick task restarted (exchange reused — no reconnect)")

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _run(self):
        """
        OPTION-A-1: Polls every TRAIL_LOOP_SEC seconds (0.1s from .env).
        OPTION-A-2: Uses self._exchange — already open, no TLS handshake.
        """
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                if not self._running:
                    break
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if current_price > 0:
                        await self._on_tick(current_price)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Tick fetch error: {e}")
        except asyncio.CancelledError:
            pass  # Normal — bar boundary cancellation; exchange stays alive

    async def _on_tick(self, current_price: float):
        if not self._running or self.risk is None or self.state is None:
            return

        # BUG-2 FIX: _bar_just_closed guard block REMOVED.
        # The old guard skipped ALL exit evaluation on the first tick after
        # each bar boundary. Pine has no equivalent suppression — strategy.exit()
        # evaluates on every tick including the first tick of each new bar.
        # Peak tracking in that guard was redundant (step 1 below covers it).

        risk, state = self.risk, self.state
        now_ms      = int(time.time() * 1000)
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # 1. Update intrabar peak
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # 2. Peak profit dist (from peak to entry)
        peak_profit_dist = max(
            0.0,
            (state.peak_price - entry_price) if is_long
            else (entry_price - state.peak_price)
        )

        # NOTE: FIX-TRAIL-7 — Stage upgrades happen exclusively at bar close
        # (on_bar_close()). Pine's calc_on_every_tick=false means the strategy
        # block only runs at bar close. Trail SL execution runs every tick here.

        # 3. TP check — BUG-1 FIX: _in_entry_guard now always returns False
        #    (ENTRY_GUARD_MS = 0), so this fires from the very first tick.
        if (is_long and current_price >= risk.tp) or \
           (not is_long and current_price <= risk.tp):
            logger.info(f"[TICK] Target Profit hit | price={current_price:.2f} tp={risk.tp:.2f}")
            await self._execute_exit(current_price, "Target Profit")
            return

        # 4. Max SL check — fires immediately (ENTRY_GUARD_MS = 0)
        if not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                logger.info(f"[TICK] Max SL hit | price={current_price:.2f}")
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # 5. Ratchet trail SL — FIX-TRAIL-12B: No activation gate (Pine-exact).
        #    Pine strategy.exit(trail_points=X) places the trailing stop at
        #    peak - trail_points from the very FIRST tick after entry.
        #    The max/min ratchet below ensures trail never widens the stop.
        trail_sl = _compute_trail_sl(
            state.stage, state.peak_price, peak_profit_dist, is_long, atr
        )
        if trail_sl is not None:
            if (is_long and trail_sl > state.current_sl) or \
               (not is_long and trail_sl < state.current_sl):
                state.current_sl = trail_sl
                logger.debug(
                    f"[TICK] Trail SL ratcheted → {state.current_sl:.2f} "
                    f"(peak={state.peak_price:.2f} peak_profit={peak_profit_dist:.2f} "
                    f"stage={state.stage})"
                )

        # 6. SL check — fires immediately (ENTRY_GUARD_MS = 0)
        if state.current_sl > 0:
            sl_hit = (
                (is_long  and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER) or
                (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_hit:
                trail_active = (
                    (is_long  and state.current_sl > risk.sl) or
                    (not is_long and state.current_sl < risk.sl)
                )
                if state.be_done and abs(state.current_sl - entry_price) < 1.0:
                    reason = "Breakeven SL"
                elif trail_active:
                    reason = f"Trail S{state.stage}"
                else:
                    reason = "Initial SL"

                logger.info(
                    f"[TICK] {reason} hit | price={current_price:.2f} "
                    f"sl={state.current_sl:.2f} stage={state.stage}"
                )
                await self._execute_exit(current_price, reason)

    # ── Guards ────────────────────────────────────────────────────────────────

    def _in_entry_guard(self, now_ms: int) -> bool:
        """
        BUG-1 FIX: ENTRY_GUARD_MS = 0, so this always returns False.
        Kept for reference; all callers removed from _on_tick() above.
        """
        if self.entry_bar_time_ms is None:
            return False
        return (now_ms - self.entry_bar_time_ms) < ENTRY_GUARD_MS

    # ── Exit execution ────────────────────────────────────────────────────────

    async def _execute_exit(self, price: float, reason: str):
        if self._exit_triggered:
            return
        self._exit_triggered = True
        self._running = False

        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(
                exit_order.get("average") or exit_order.get("price") or price
            )
        except Exception as e:
            logger.error(f"close_at_trail_sl failed: {e}")
            fill_price = price

        logger.info(
            f"Exit executed | reason={reason} "
            f"price={price:.2f} fill={fill_price:.2f}"
        )

        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)
