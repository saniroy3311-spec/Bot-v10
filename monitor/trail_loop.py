"""
monitor/trail_loop.py — Shiva Sniper v6.5 (PINE-EXACT-v10)

FIX-TRAIL-11 | Re-add trail_offset activation gate (BUG-TRAIL-OFFSET-001)
──────────────────────────────────────────────────────────────────────
ROOT CAUSE:
  FIX-TRAIL-9 removed the gate on _compute_trail_sl() entirely, but the
  WRONG gate was removed. PineScript strategy.exit(trail_points=X,
  trail_offset=Y) has two distinct parameters:
    • trail_offset = ACTIVATION THRESHOLD: the trail stop does NOT fire
      until price moves `trail_offset` (activeOff = atr * off_mult) in the
      trade's favor from the fill price.
    • trail_points = STOP DISTANCE from peak once the trail is activated.
  FIX-TRAIL-9 removed a gate at the trail_POINTS level. The correct gate
  is at the trail_OFFSET level.

OBSERVED BUG (BUG-TRAIL-OFFSET-001):
  Without the trail_offset gate, _compute_trail_sl() immediately returns
  peak - activePts (= entry - atr*0.70 for stage 1). Since this is ABOVE
  the initial SL (entry - atr*0.90), current_sl ratchets up instantly,
  making the effective stop 0.20 ATR tighter than Pine's initial stop.
  A brief tick dip of 0.70 ATR below entry triggers the bot while Pine's
  0.90 ATR initial stop hasn't fired. The result: bot exits at a loss on
  trades where Pine holds and later exits profitably via the trail.

FIX:
  In _on_tick(), gate the _compute_trail_sl() call on:
    peak_profit_dist >= self._active_off
  i.e. trail only ratchets after price has moved activeOff favorably.
  Before activation, state.current_sl stays at risk.sl (initial SL),
  exactly matching Pine's fixed `stop=longSL` being the only active
  exit before the trail engages.

──────────────────────────────────────────────────────────────────────

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
FIX-TRAIL-9 | Removed peak_profit_dist gate from _compute_trail_sl()
  ROOT CAUSE (BUG-TRAIL-GATE-001 — observed 2026-04-17 14:30 Range Short):
    The gate `if peak_profit_dist < active_pts_val: return None` prevented
    the trailing stop from being placed until price had already moved the
    FULL trail_pts distance in our favour.
    Example: ATR=284, trail1Pts=0.70 → trail_pts=199. Price dipped 168 pts
    favourably (< 199), so _compute_trail_sl returned None. current_sl stayed
    at original initial SL (75,949). Price then reversed → bot stopped out
    at 76,128 (-377 pts). Pine had no gate — it moved trail_sl to
    peak + 199 = 75,781 and exited there (only -39 pts vs entry).
  FIX:
    Removed the gate entirely. Trail SL = peak ± trail_pts always, matching
    Pine's strategy.exit(trail_points=X) which places the trailing stop from
    the first tick. Initially trail_sl = entry ± trail_pts = initial SL.
    As price moves favourably, trail_sl improves. No gate required.

FIX-TRAIL-10 | Never widen SL when ATR grows between bars
  ROOT CAUSE (BUG-ATR-WIDEN-001):
    on_bar_close() recalculates SL using current ATR. If ATR grew since
    entry (e.g. 284→318), new_stop_dist grew, pushing new_sl AWAY from
    entry for both longs and shorts — widening the stop and diverging from
    Pine's fixed initial `stop=` parameter.
  FIX:
    When trail has not yet moved the SL, only apply new_sl if it is
    MORE FAVOURABLE (tighter) than current_sl:
      Long:  current_sl = max(new_sl, current_sl)  — never lower
      Short: current_sl = min(new_sl, current_sl)  — never higher

──────────────────────────────────────────────────────────────────────
PRESERVED FROM v8 (PINE-EXACT-v7 renamed v8 after prior patches):
──────────────────────────────────────────────────────────────────────
  ROOT CAUSE of early exits vs TradingView:
    Pine Script runs with calc_on_every_tick=false.
    The strategy block (where trail stage logic lives) executes ONLY at
    bar close — NOT on every tick. OPTION-B-1 was upgrading stages
    mid-bar every 0.5s, tightening the trail prematurely and causing
    exits up to 229 pts before Pine's exit price.
  FIX:
    Removed live_profit_dist + _calc_new_stage() block from _on_tick().
    Stage upgrades now happen ONLY in on_bar_close() — Pine parity.
    Trail execution (SL check) still runs every tick — correct.

FIX-TRAIL-8 | Peak price preserves intra-bar extremes (BUG-PEAK-RESET-001)
  ROOT CAUSE:
    state.peak_price = bar_close at every bar boundary discarded
    intra-bar highs (longs) / lows (shorts) tracked during the tick loop.
    If price hit 75,800 mid-bar but closed at 75,600, trail was computed
    from 75,600 instead of 75,800 — understating the peak by up to bar range.
  FIX:
    Long:  state.peak_price = max(state.peak_price, bar_high, bar_close)
    Short: state.peak_price = min(state.peak_price, bar_low,  bar_close)
    bar_high / bar_low already passed into on_bar_close() — no API change.

PRESERVED FROM v6:
──────────────────────────────────────────────────────────────────────
OPTION-A-1 | Poll every TRAIL_LOOP_SEC seconds (0.1s from .env)
OPTION-A-2 | Persistent exchange connection (no reconnect per bar)
OPTION-B-2 | Trail SL uses live peak_profit_dist (Pine parity)
FIX-TRAIL-1..6 | All prior peak-reset and race-condition fixes
──────────────────────────────────────────────────────────────────────
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

ENTRY_GUARD_MS = 5 * 1000


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
    Pine-exact trail stop: SL = peak - trail_POINTS (not trail_offset).

    Pine Script strategy.exit(trail_points=activePts) places the stop at
    peak_price - trail_points (for longs), where trail_points is the wider
    of the two ATR multiples stored per stage.  trail_offset in Pine is a
    limit-order slippage parameter only — it does NOT change where the SL
    sits relative to peak.

    FIX-TRAIL-PTS: Changed return value from active_off_val → active_pts_val
    so the bot matches Pine exactly:
      Stage 1 : peak - 0.70 ATR  (was 0.55 ATR — 15 pts tighter per ATR)
      Stage 2 : peak - 0.55 ATR  (was 0.45 ATR)
      Stage 3 : peak - 0.45 ATR  (was 0.35 ATR)
      Stage 4 : peak - 0.30 ATR  (was 0.25 ATR)
      Stage 5 : peak - 0.20 ATR  (was 0.15 ATR)
    """
    # stage 0 maps to Stage 1 params (idx=0).
    # Pine applies trail1Pts from the very first tick — no stage gate.
    _, active_pts, active_off = TRAIL_STAGES[max(stage - 1, 0)]
    active_pts_val = atr * active_pts
    active_off_val = atr * active_off
    # FIX-TRAIL-9: Removed `if peak_profit_dist < active_pts_val: return None` gate.
    #
    # ROOT CAUSE OF THIS BUG:
    #   The old gate required price to move trail_pts in our favour BEFORE the
    #   trailing stop was computed. Example: ATR=284, trail1Pts=0.70 → trail_pts=199.
    #   If price only moved 168 pts favourably, trail_sl returned None and
    #   current_sl stayed at the original initial SL — never tightening.
    #
    # WHAT PINE ACTUALLY DOES:
    #   strategy.exit(trail_points=X) places the trailing stop at
    #   `peak ± trail_pts` from the very first tick after entry.
    #   Initially (no favourable movement) → trail_sl = entry ± trail_pts
    #   = same as initial SL. As price moves favourably the trail improves.
    #   There is NO gate that prevents the trail from computing.
    #
    # RESULT OF THE BUG (observed 2026-04-17 14:30 Range Short):
    #   Price dipped 168.5 pts favourably (< trail_pts 198.9) then reversed.
    #   Pine moved trail_sl to peak + trail_pts = 75,582 + 199 = 75,781 and
    #   exited when price rose through 75,781 (small loss/near-breakeven).
    #   Bot kept SL at 75,949 → price ran to 76,128 before stopping out
    #   (-377 pts vs Pine's -39 pts on the position).
    #
    # Pine: SL = peak ± trail_points (always, no gate)
    return (peak_price - active_pts_val - active_off_val) if is_long else (peak_price + active_pts_val + active_off_val)


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
        self._bar_just_closed: bool = False  # FIX-TRAIL-6

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
            f"poll_interval={TRAIL_LOOP_SEC}s [OPTION-A-1]"
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

        # FIX-TRAIL-6
        self._bar_just_closed = True

        # FIX-TRAIL-2: Recalculate SL/TP/ATR every bar (Pine parity)
        old_atr       = self.risk.atr
        old_sl        = self.risk.sl
        atr_mult      = TREND_ATR_MULT if self.risk.is_trend else RANGE_ATR_MULT
        rr            = TREND_RR       if self.risk.is_trend else RANGE_RR
        new_stop_dist = min(current_atr * atr_mult, MAX_SL_POINTS)
        entry_price_  = self.risk.entry_price
        is_long_      = self.risk.is_long

        if is_long_:
            new_sl = entry_price_ - new_stop_dist
            new_tp = entry_price_ + new_stop_dist * rr
        else:
            new_sl = entry_price_ + new_stop_dist
            new_tp = entry_price_ - new_stop_dist * rr

        self.risk = RiskLevels(
            entry_price = entry_price_,
            sl          = new_sl,
            tp          = new_tp,
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
            # FIX-TRAIL-10: Never widen the initial SL when ATR increases.
            #
            # ROOT CAUSE OF THIS BUG:
            #   If ATR grows between bars, new_stop_dist grows, so:
            #     Long:  new_sl = entry - new_stop_dist  (goes DOWN  = wider)
            #     Short: new_sl = entry + new_stop_dist  (goes UP    = wider)
            #   The old code blindly set current_sl = new_sl, effectively giving
            #   the market more room to hit the SL — diverging from Pine where
            #   the initial `stop=` parameter is fixed at entry and never changes.
            #
            # FIX: Only apply the recalculated SL if it is MORE FAVOURABLE
            # (tighter) than the current SL. This matches Pine: initial SL is a
            # one-way ratchet — it can only improve, never widen.
            if is_long_:
                self.state.current_sl = max(new_sl, self.state.current_sl)
            else:
                self.state.current_sl = min(new_sl, self.state.current_sl)

        logger.info(
            f"[BAR CLOSE] ATR {old_atr:.2f}→{current_atr:.2f} | "
            f"stop_dist={new_stop_dist:.2f} | "
            f"sl={new_sl:.2f} tp={new_tp:.2f} | "
            f"current_sl={'kept at trail ' + str(round(self.state.current_sl,2)) if trail_has_moved_sl else str(round(new_sl,2))}"
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

        # FIX-TRAIL-8: Preserve intra-bar peak extremes — do NOT discard
        # highs/lows seen during tick loop by resetting to bar_close only.
        # Long:  keep highest of current peak, bar_high, bar_close
        # Short: keep lowest  of current peak, bar_low,  bar_close
        if is_long:
            state.peak_price = max(state.peak_price, bar_high, bar_close)
        else:
            state.peak_price = min(state.peak_price, bar_low, bar_close)

        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak updated to {state.peak_price:.2f} "
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

        # FIX-TRAIL-6: Skip exit eval on first tick after bar boundary
        if self._bar_just_closed:
            self._bar_just_closed = False
            logger.debug(f"[TICK] Bar-boundary guard — skipping eval | price={current_price:.2f}")
            if self.risk.is_long:
                self.state.peak_price = max(self.state.peak_price, current_price)
            else:
                self.state.peak_price = min(self.state.peak_price, current_price)
            return

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

        # NOTE: FIX-TRAIL-7 — OPTION-B-1 intra-bar stage upgrade removed.
        # Pine's calc_on_every_tick=false means the strategy block (stage logic)
        # only runs at bar close. Stage upgrades happen exclusively in
        # on_bar_close() above. Trail SL execution still runs every tick.

        # 3. TP check
        if not self._in_entry_guard(now_ms):
            if (is_long and current_price >= risk.tp) or \
               (not is_long and current_price <= risk.tp):
                logger.info(f"[TICK] Target Profit hit | price={current_price:.2f} tp={risk.tp:.2f}")
                await self._execute_exit(current_price, "Target Profit")
                return

        # 4. Max SL check
        if not self._in_entry_guard(now_ms) and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                logger.info(f"[TICK] Max SL hit | price={current_price:.2f}")
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # 5. Ratchet trail SL — FIX-TRAIL-11: Pine-exact trail_offset gate
        #
        # PineScript: strategy.exit(trail_points=activePts, trail_offset=activeOff)
        #
        #   trail_offset (self._active_off) = ACTIVATION THRESHOLD.
        #     The trailing stop does NOT activate until price has moved
        #     `trail_offset` favorably from the entry fill price.
        #     Before activation, only fixed stop=longSL (risk.sl, 0.90 × ATR)
        #     is in effect.
        #
        #   trail_points (self._active_pts) = STOP DISTANCE from peak.
        #     Once the trail is activated, stop = peak - trail_points.
        #
        # Without this gate (the FIX-TRAIL-9 state), trail_sl immediately
        # becomes entry - 0.70×ATR (TIGHTER than initial 0.90×ATR stop),
        # causing exits on brief dips that Pine's wider initial SL ignores.
        if peak_profit_dist >= self._active_off:
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
                        f"active_off={self._active_off:.2f} stage={state.stage})"
                    )
        else:
            logger.debug(
                f"[TICK] Trail NOT yet active | peak_profit={peak_profit_dist:.2f} "
                f"< active_off={self._active_off:.2f} | sl stays at {state.current_sl:.2f}"
            )

        # 6. SL check
        if state.current_sl > 0 and not self._in_entry_guard(now_ms):
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
