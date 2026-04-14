"""
monitor/trail_loop.py — Shiva Sniper v6.5 (PINE-EXACT-v6)

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
OPTION-A-1 | Poll every 0.5 seconds (via TRAIL_LOOP_SEC in .env)
  Tightest reliable interval on Delta India REST without rate limits.

OPTION-A-2 | Persistent exchange connection (no reconnect per bar)
  ROOT CAUSE of previous latency:
    _run() created ccxt.delta({...}) INSIDE the loop.
    on_bar_close() cancels + restarts _run() at every 30m bar close.
    Each restart triggered a fresh TLS handshake (~200-500ms blind window).
  FIX:
    Exchange created ONCE in start(), stored as self._exchange.
    _run() reuses it. on_bar_close() restarts only the coroutine.
    Only closed in stop(). Zero reconnect latency at bar boundaries.

OPTION-B-1 | Intra-bar stage upgrade on every tick (Pine parity)
  ROOT CAUSE of missed exits:
    Old code upgraded stage ONLY in on_bar_close() using bar CLOSE price.
    Pine's stage upgrade block runs on EVERY tick. If price crosses a
    stage trigger mid-bar, Pine tightens the trail immediately.
    Old bot waited up to 30 minutes — exiting at the wrong price.
  FIX:
    _on_tick() calls _calc_new_stage(live_profit_dist, atr) every tick.
    Stage upgrades instantly when price crosses the trigger threshold.
    active_pts and active_off recalculated immediately.

OPTION-B-2 | Trail SL uses live peak_profit_dist (Pine parity)
  Pine guard: trail only moves stop when peak moved by trail_points.
  Now uses the live peak_profit_dist computed this tick, not stale value.

PRESERVED FROM v5:
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
    Called on EVERY tick (OPTION-B-1) AND at bar close — Pine parity.
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
    OPTION-B-2: Trail stop = peak - trail_offset (long) or peak + trail_offset (short).
    Only fires when peak_profit_dist >= active_pts (Pine implicit guard).
    """
    if stage == 0:
        return None
    _, active_pts, active_off = TRAIL_STAGES[stage - 1]
    active_pts_val = atr * active_pts
    active_off_val = atr * active_off
    if peak_profit_dist < active_pts_val:
        return None
    return (peak_price - active_off_val) if is_long else (peak_price + active_off_val)


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
            self.state.current_sl = new_sl

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

        # FIX-TRAIL-1: Reset peak to bar_close
        state.peak_price = bar_close
        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak reset to bar_close={bar_close:.2f} "
            f"current_sl={state.current_sl:.2f} be_done={state.be_done} atr={atr:.2f}"
        )

        # FIX-TRAIL-5 + OPTION-A-2: Restart task coroutine, NOT the exchange
        if self._running:
            self._task = asyncio.create_task(self._run())
            logger.debug("[BAR CLOSE] Tick task restarted (exchange reused — no reconnect)")

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _run(self):
        """
        OPTION-A-1: Polls every TRAIL_LOOP_SEC seconds (0.5s from .env).
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

        # OPTION-B-1: Intra-bar stage upgrade — runs EVERY tick like Pine
        live_profit_dist = max(
            0.0,
            (current_price - entry_price) if is_long
            else (entry_price - current_price)
        )
        new_stage = _calc_new_stage(live_profit_dist, atr)
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            self._active_pts, self._active_off = _get_active_params(new_stage, atr)
            logger.info(
                f"[TICK] Trail stage {old_stage}→{new_stage} INTRA-BAR "
                f"| price={current_price:.2f} profit={live_profit_dist:.2f} "
                f"active_off={self._active_off:.2f}"
            )

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

        # 5. Ratchet trail SL (OPTION-B-2: live peak_profit_dist)
        if state.stage > 0:
            trail_sl = _compute_trail_sl(
                state.stage, state.peak_price, peak_profit_dist, is_long, atr
            )
            if trail_sl is not None:
                if (is_long and trail_sl > state.current_sl) or \
                   (not is_long and trail_sl < state.current_sl):
                    state.current_sl = trail_sl

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
