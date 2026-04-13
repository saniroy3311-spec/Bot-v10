"""
monitor/trail_loop.py - Shiva Sniper v6.5 (PINE-EXACT-v3)

ARCHITECTURE — matches Pine Script strategy.exit() exactly:

  BAR CLOSE  → on_bar_close(bar_close, bar_high, bar_low)
               • Upgrades trail STAGE (1→2→3→4→5) using bar close profit dist
               • Updates activePts / activeOff for the new stage
               • Moves breakeven SL if threshold crossed at bar close
               ─ This is ALL that happens at bar close ─

  EVERY TICK → _on_tick(current_price)   [runs every TRAIL_LOOP_SEC = 0.1–0.5s]
               • Updates peak_price using live price (intrabar high/low tracking)
               • Ratchets trail_sl = peak_price - active_off  (or + for shorts)
               • Fires exit immediately when price crosses trail_sl
               • Fires exit immediately when price crosses initial SL or TP
               • Fires Max SL exit immediately

WHY THIS MATCHES PINE:
  Pine's strategy.exit(trail_points=X, trail_offset=Y) creates a live bracket
  on the broker emulator that tracks price every tick. Stage parameters change
  only on bar close (profitDist uses close), but the stop price itself tracks
  every intrabar tick. Exits fire mid-candle — often within 40s of the peak.

FIXES vs previous version:
  PINE-FIX-001 | on_bar_close() now exists — main.py no longer crashes
  PINE-FIX-002 | Stage upgrade uses bar CLOSE profit dist (not tick price)
  PINE-FIX-003 | peak_price updated every TICK from live price (intrabar)
  PINE-FIX-004 | Trail SL fires from tick loop — mid-candle, like Pine
  PINE-FIX-005 | on_bar_close also updates peak with bar_high/bar_low
                 (catches any high/low the tick loop may have missed)
  PINE-FIX-006 | Breakeven check moved to bar close (matches Pine's close check)
  PINE-FIX-007 | Max SL still fires from tick loop (emergency, matches Pine emulator)
  PINE-FIX-008 | Removed duplicate notify_exit from _execute_exit —
                 single notification path via on_trail_exit callback in main.py
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
)

logger = logging.getLogger(__name__)

# Block exits for 30s after fill — avoids noise on entry bar
ENTRY_GUARD_MS = 30 * 1000


# ─── Stage helpers ────────────────────────────────────────────────────────────

def _get_active_params(stage: int, atr: float):
    """Return (trail_points, trail_offset) for the given stage."""
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def _calc_new_stage(profit_dist: float, atr: float) -> int:
    """
    Return the highest stage whose trigger is satisfied by profit_dist.
    Mirrors Pine's if/else-if chain evaluated at bar close.
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
    Compute the current trail stop price.
    Returns None if stage == 0 (trail not yet active).
    Mirrors Pine: trail stop = peak - trail_offset  (long)
                              = peak + trail_offset  (short)
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
    """
    Tick-level exit monitor replicating Pine Script strategy.exit() exactly.

    Call flow:
        start()          — called once when position opens
        on_bar_close()   — called by main.py on every 30m bar close
        _on_tick()       — runs internally every TRAIL_LOOP_SEC seconds
        stop()           — called when position is closed
    """

    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal

        self.risk:  Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None

        self._running         = False
        self._task: Optional[asyncio.Task] = None
        self._exchange        = None
        self._exit_triggered  = False

        # Callback fired immediately when exit executes (tick-resolution)
        self.on_trail_exit: Optional[Callable] = None

        # Timestamp of entry fill — used for entry guard
        self.entry_bar_time_ms: Optional[int] = None

        # Active trail parameters — updated by on_bar_close, read by tick loop
        # These are the Pine activePts / activeOff for the current stage
        self._active_pts: float = 0.0
        self._active_off: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def start(
        self,
        risk_levels: RiskLevels,
        trail_state: TrailState,
        entry_bar_time_ms: Optional[int] = None,
        on_trail_exit: Optional[Callable] = None,
    ):
        """
        Start monitoring. Called once after entry fill.
        """
        self.risk             = risk_levels
        self.state            = trail_state
        self.on_trail_exit    = on_trail_exit
        self._exit_triggered  = False
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)

        # Initialise peak to entry price if not already set
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        # Stage 0 — no trail active yet; set initial params so tick loop
        # has valid values (they won't fire until stage upgrades)
        self._active_pts, self._active_off = _get_active_params(0, risk_levels.atr)

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long}"
        )

    def stop(self):
        """Stop the tick loop."""
        self._running = False
        if self._task:
            self._task.cancel()

    def on_bar_close(self, bar_close: float, bar_high: float, bar_low: float):
        """
        Called by main.py at every 30-minute bar close.

        PINE-FIX-001: This method must exist — main.py calls it every bar.
        PINE-FIX-002: Stage upgrade uses BAR CLOSE profit dist (not tick).
                      Pine's profitDist = close - entryPrice (bar close value).
        PINE-FIX-005: Peak updated with bar_high / bar_low to catch any
                      intrabar extremes the tick loop may have missed between polls.
        PINE-FIX-006: Breakeven check at bar close matches Pine's close-based check.
        """
        if not self._running or self.risk is None or self.state is None:
            return

        risk, state = self.risk, self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # ── PINE-FIX-005: update peak with confirmed bar high/low ─────────────
        # The tick loop polls every 0.1–0.5s and may miss the exact bar extreme.
        # bar_high / bar_low are the confirmed OHLC values — use them to correct.
        if is_long:
            state.peak_price = max(state.peak_price, bar_high)
        else:
            state.peak_price = min(state.peak_price, bar_low)

        # ── PINE-FIX-002: stage upgrade uses bar CLOSE profit dist ────────────
        # Pine: profitDist = close - entryPrice   (evaluated at bar close)
        close_profit_dist = (bar_close - entry_price) if is_long else (entry_price - bar_close)
        close_profit_dist = max(0.0, close_profit_dist)

        new_stage = max(state.stage, _calc_new_stage(close_profit_dist, atr))
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            # Update the active params so the tick loop uses new stage offsets
            self._active_pts, self._active_off = _get_active_params(new_stage, atr)
            logger.info(
                f"[BAR CLOSE] Trail stage {old_stage}→{new_stage} "
                f"| close={bar_close:.2f} profit_dist={close_profit_dist:.2f} "
                f"active_off={self._active_off:.2f}"
            )
            # Fire Telegram in a task so we don't block the bar-close handler
            asyncio.create_task(
                self.telegram.notify_trail_stage(
                    old_stage, new_stage, bar_close, state.current_sl
                )
            )
        else:
            # Even if stage didn't change, keep _active_pts/_active_off current
            self._active_pts, self._active_off = _get_active_params(state.stage, atr)

        # ── PINE-FIX-006: breakeven at bar close ─────────────────────────────
        # Pine: if close - entryPrice > beTrigger → move SL to entryPrice
        if not state.be_done and close_profit_dist > atr * BE_MULT:
            state.be_done = True
            if (is_long and entry_price > state.current_sl) or \
               (not is_long and entry_price < state.current_sl):
                state.current_sl = entry_price
                logger.info(
                    f"[BAR CLOSE] Breakeven SL set to entry={entry_price:.2f} "
                    f"| close_profit_dist={close_profit_dist:.2f}"
                )
                asyncio.create_task(
                    self.telegram.notify_breakeven(entry_price)
                )

        logger.info(
            f"[BAR CLOSE] stage={state.stage} peak={state.peak_price:.2f} "
            f"current_sl={state.current_sl:.2f} be_done={state.be_done}"
        )

    # ── Internal tick loop ────────────────────────────────────────────────────

    async def _run(self):
        _base_url = (
            "https://testnet-api.india.delta.exchange"
            if DELTA_TESTNET
            else "https://api.india.delta.exchange"
        )
        self._exchange = ccxt.delta({
            "apiKey"        : DELTA_API_KEY,
            "secret"        : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls"          : {"api": {"public": _base_url, "private": _base_url}},
        })
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    current_price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if current_price > 0:
                        await self._on_tick(current_price)
                except Exception as e:
                    logger.warning(f"Tick fetch error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    async def _on_tick(self, current_price: float):
        """
        Runs every TRAIL_LOOP_SEC (0.1–0.5s).

        PINE-FIX-003: peak_price updated from LIVE TICK every loop.
                      Pine's broker emulator tracks high/low every tick.
        PINE-FIX-004: Trail SL fires mid-candle from this tick loop.
                      This is why Pine exits in < 1 minute — not at bar close.
        """
        if not self._running or self.risk is None or self.state is None:
            return

        risk, state   = self.risk, self.state
        now_ms        = int(time.time() * 1000)
        is_long       = risk.is_long
        entry_price   = risk.entry_price
        atr           = risk.atr

        # ── 1. Update intrabar peak (tick-by-tick, matches Pine emulator) ─────
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # ── 2. Compute peak profit dist (used for trail SL calculation) ───────
        peak_profit_dist = max(
            0.0,
            (state.peak_price - entry_price) if is_long
            else (entry_price - state.peak_price)
        )

        # ── 3. TP check (fires mid-candle, matches Pine broker emulator) ──────
        if not self._in_entry_guard(now_ms):
            if (is_long and current_price >= risk.tp) or \
               (not is_long and current_price <= risk.tp):
                logger.info(f"[TICK] Target Profit hit | price={current_price:.2f} tp={risk.tp:.2f}")
                await self._execute_exit(current_price, "Target Profit")
                return

        # ── 4. Max SL check (emergency — fires from tick loop) ────────────────
        # Pine's Max SL is checked every bar close, but in a live bot we fire it
        # immediately on tick to avoid catastrophic loss. This is the ONE case
        # where we deviate from bar-close timing intentionally.
        if not self._in_entry_guard(now_ms) and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                logger.info(f"[TICK] Max SL hit | price={current_price:.2f}")
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # ── 5. Ratchet trail SL using current stage params ────────────────────
        # _active_off is set by on_bar_close() when stage upgrades.
        # Between bar closes, stage stays fixed — only peak moves.
        if state.stage > 0:
            trail_sl = _compute_trail_sl(
                state.stage, state.peak_price, peak_profit_dist, is_long, atr
            )
            if trail_sl is not None:
                # Ratchet — trail SL only moves in the profitable direction
                if (is_long and trail_sl > state.current_sl) or \
                   (not is_long and trail_sl < state.current_sl):
                    state.current_sl = trail_sl

        # ── 6. SL check — all SL types evaluated every tick ──────────────────
        # This is the key: Pine's broker emulator checks EVERY TICK.
        # Initial SL, Breakeven SL, Trail SL — all fire mid-candle.
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

    # ── Exit execution ────────────────────────────────────────────────────────

    def _in_entry_guard(self, now_ms: int) -> bool:
        if self.entry_bar_time_ms is None:
            return False
        return (now_ms - self.entry_bar_time_ms) < ENTRY_GUARD_MS

    async def _execute_exit(self, price: float, reason: str):
        """
        Place the close order and fire the on_trail_exit callback.

        PINE-FIX-008: notify_exit is NOT called here.
                      Single notification path: main._on_trail_exit()
                      which has the correct entry_price and is_long context.
        """
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

        # Fire callback in main.py — it handles journal + Telegram notification
        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)
