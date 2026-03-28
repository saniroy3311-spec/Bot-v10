"""
monitor/trail_loop.py  — FIXED (bugs B1, B2, B3)

FIXES APPLIED:
──────────────────────────────────────────────────────────────────────────────
B1: _loop_ws() was a `pass` stub — the task returned instantly, _on_tick
    was never called, position was never monitored.
    FIX: Replaced with _loop_rest() — polls Delta ticker every TRAIL_LOOP_SEC.
    A WebSocket variant is wired in as a drop-in upgrade path below.

B2: SL distance used trail_off (limit buffer) instead of trail_pts (SL distance).
    FIX: candidate_sl = peak_price - trail_pts (long) / + trail_pts (short).
    Also: modify_sl() now receives trail_off as sl_limit_buf (ATR-dynamic),
    matching the intent documented in orders/manager.py FIX-3.

B3: should_trigger_be() and max_sl_hit() were imported but never called.
    FIX: Both are called inside _on_tick() at the correct priority:
      1. Max SL check  (highest — hard floor)
      2. Breakeven     (one-shot, only if not done)
      3. Trail ratchet (ongoing)
──────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt

from risk.calculator import (
    TrailState,
    RiskLevels,
    calc_trail_stage,
    get_trail_params,
    should_trigger_be,
    max_sl_hit,
    calc_real_pl,
)
from config import (
    TRAIL_LOOP_SEC,
    ALERT_QTY,
    SYMBOL,
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_TESTNET,
)

logger = logging.getLogger(__name__)


class TrailMonitor:
    """
    Monitor an open position every TRAIL_LOOP_SEC seconds via REST ticker.

    Priority inside each tick (mirrors Pine Script exit priority):
        1. Max SL   — emergency close if loss exceeds hard cap
        2. Breakeven — move SL to entry once profit threshold crossed (once only)
        3. Trail     — ratchet SL toward peak as profit grows
    """

    def __init__(self, order_manager, telegram, journal):
        self.order_mgr  = order_manager
        self.telegram   = telegram
        self.journal    = journal
        self.risk:  Optional[RiskLevels]  = None
        self.state: Optional[TrailState] = None
        self._running   = False
        self._task: Optional[asyncio.Task] = None
        self._exchange: Optional[ccxt.delta] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, risk_levels: RiskLevels, trail_state: TrailState) -> None:
        """Called by main.py after a confirmed entry fill."""
        self.risk  = risk_levels
        self.state = trail_state
        # Initialise peak to entry price (will update on first tick)
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price
        self._running = True
        self._task    = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor started | entry={risk_levels.entry_price:.2f} "
            f"sl={risk_levels.sl:.2f} tp={risk_levels.tp:.2f} "
            f"atr={risk_levels.atr:.2f} long={risk_levels.is_long}"
        )

    def stop(self) -> None:
        """Called by main.py when position is detected closed."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("TrailMonitor stopped.")

    # ── Internal run loop ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Entry point for the monitoring task."""
        try:
            await self._loop_rest()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TrailMonitor crashed: {e}", exc_info=True)

    async def _loop_rest(self) -> None:
        """
        B1 FIX: REST ticker poll loop.
        Replaces the dead `pass` stub that was here before.

        Polls Delta Exchange ticker every TRAIL_LOOP_SEC seconds.
        Uses a shared ccxt exchange instance (lazy-created, closed on stop).

        Upgrade path: swap this method for a WebSocket subscriber
        (Delta WS `/v2/trades` or mark-price feed) without changing
        any other code — just call `await self._on_tick(mark_price)`.
        """
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
        }
        self._exchange = ccxt.delta(params)
        if DELTA_TESTNET:
            self._exchange.set_sandbox_mode(True)

        logger.info(f"Trail ticker polling every {TRAIL_LOOP_SEC}s for {SYMBOL}")

        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)

                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    mark_price = float(
                        ticker.get("info", {}).get("mark_price") or
                        ticker.get("last") or
                        0
                    )
                    if mark_price > 0:
                        await self._on_tick(mark_price)
                    else:
                        logger.warning("Ticker returned zero/null price — skipping tick")

                except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                    logger.warning(f"Ticker fetch failed (network): {e}")
                except Exception as e:
                    logger.error(f"Tick processing error: {e}", exc_info=True)
        finally:
            if self._exchange:
                await self._exchange.close()

    # ── Core tick logic ───────────────────────────────────────────────────────

    async def _on_tick(self, current_price: float) -> None:
        """
        Process one price tick.

        Priority (matches Pine Script):
          1. Max SL   → emergency close, return
          2. Breakeven → move SL to entry (once)
          3. Trail     → ratchet SL toward peak
        """
        if not self._running or self.risk is None or self.state is None:
            return

        risk  = self.risk
        state = self.state

        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr  # entry-bar ATR — matches Pine Script

        # ── 1. Max SL ─────────────────────────────────────────────────────────
        # B3 FIX: was never called before
        if not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
            logger.warning(
                f"MAX SL HIT | price={current_price:.2f} entry={entry_price:.2f} "
                f"atr={atr:.2f} long={is_long}"
            )
            state.max_sl_fired = True
            self._running = False
            await self.telegram.notify_max_sl(current_price, entry_price) # Added notify
            try:
                await self.order_mgr.close_position(reason="Max SL Hit")
            except Exception as e:
                logger.error(f"Emergency close failed: {e}", exc_info=True)
            return

        # ── Update peak price ─────────────────────────────────────────────────
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        profit_dist = abs(state.peak_price - entry_price)

        # ── 2. Breakeven ──────────────────────────────────────────────────────
        # B3 FIX: was never called before
        if not state.be_done and should_trigger_be(profit_dist, atr):
            logger.info(
                f"BREAKEVEN triggered | profit={profit_dist:.2f} "
                f"threshold={atr * 0.6:.2f} | SL → entry={entry_price:.2f}"
            )
            state.be_done     = True
            state.current_sl  = entry_price
            await self.telegram.notify_breakeven(entry_price) # Added notify
            try:
                # Use flat buffer for BE (ATR-dynamic not yet critical at this stage)
                from config import BRACKET_SL_BUFFER
                await self.order_mgr.modify_sl(entry_price, BRACKET_SL_BUFFER)
            except Exception as e:
                logger.error(f"Breakeven SL modify failed: {e}", exc_info=True)

        # ── 3. Trail ratchet ──────────────────────────────────────────────────
        new_stage = calc_trail_stage(profit_dist, atr)
        if new_stage > state.stage:
            logger.info(f"TRAIL stage {state.stage} → {new_stage}")
            await self.telegram.notify_trail_stage(state.stage, new_stage, current_price, state.current_sl) # Added notify
            state.stage = new_stage

        if state.stage > 0:
            # B2 FIX: trail_pts is the SL distance from peak (not trail_off)
            trail_pts, trail_off = get_trail_params(state.stage, atr)

            # Activation gate: only ratchet once profit exceeds trail_pts
            if profit_dist >= trail_pts:
                if is_long:
                    candidate_sl = state.peak_price - trail_pts  # FIX: was trail_off
                else:
                    candidate_sl = state.peak_price + trail_pts  # FIX: was trail_off

                if self._sl_improved(candidate_sl):
                    logger.info(
                        f"TRAIL SL update | stage={state.stage} "
                        f"peak={state.peak_price:.2f} pts={trail_pts:.2f} "
                        f"new_sl={candidate_sl:.2f}"
                    )
                    state.current_sl = candidate_sl
                    try:
                        # B2 FIX: pass trail_off as ATR-dynamic limit buffer
                        # (matches Pine Script bracket logic; FIX-3 in orders/manager.py)
                        await self.order_mgr.modify_sl(candidate_sl, trail_off)
                    except Exception as e:
                        logger.error(f"Trail SL modify failed: {e}", exc_info=True)

        # ── Dashboard sync ────────────────────────────────────────────────────
        try:
            self.journal.update_open_trade(
                trail_stage = state.stage,
                current_sl  = state.current_sl,
                peak_price  = state.peak_price,
            )
        except Exception as e:
            logger.debug(f"Dashboard sync skipped: {e}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _sl_improved(self, new_sl: float) -> bool:
        """True if new_sl is strictly better than current (monotonic ratchet)."""
        if self.risk is None or self.state is None:
            return False
        if self.risk.is_long:
            return new_sl > self.state.current_sl
        else:
            return new_sl < self.state.current_sl
