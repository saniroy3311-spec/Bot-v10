"""
monitor/trail_loop.py

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-TRAIL-OFFSET | calc_trail_stage() now activates at trail1Off (0.55×ATR)
                   not trail1Trigger (1.0×ATR), exactly matching PineScript's
                   trail_offset parameter in strategy.exit().

                   Root cause of trade mismatch:
                   • Pine trail activates when profit ≥ 0.55×ATR
                   • Old bot trail activated when profit ≥ 1.00×ATR
                   • In trade 2 (15:00 entry), price only reached ~0.73×ATR
                     profit before reversing → Pine exited via trail at ~69935,
                     bot never set a trail SL and held to Max SL at 69239.

FIX-TRAIL-GATE   | Removed `profit_dist >= trail_pts` gate in the trail ratchet.
                   PineScript places the trailing stop at peak - trail_points
                   immediately when trail activates — no second condition.
                   The gate was blocking SL updates in the 0.55→0.70 ATR range.

FIX-BE-LOG       | Hardcoded `atr * 0.6` in the BE log message replaced with
                   `atr * BE_MULT` (reads from config, currently 1.0).

PRESERVED FIXES (from previous version):
  FIX-EXIT | Trail SL breach → market close (mirrors Pine strategy.exit trail)
  FIX-BE   | Breakeven SL tracked in state.current_sl
  FIX-TP   | Bracket TP is safety-net; primary exit is trail SL breach
  B1-B3    | REST ticker poll, trail_pts for SL distance, max_sl + BE calls
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
    BE_MULT,
)

logger = logging.getLogger(__name__)


class TrailMonitor:
    """
    Monitor an open position every TRAIL_LOOP_SEC seconds via REST ticker.

    Exit priority (mirrors Pine Script strategy.exit() exactly):
        1. Max SL       — hard loss cap → emergency market close
        2. Trail SL     — price crossed current_sl → market close  ← FIX-EXIT
        3. Breakeven    — move SL to entry once (one-shot)
        4. Trail ratchet — advance current_sl as peak moves
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
        # Callback set by main.py so trail_loop can signal position close
        self.on_trail_exit = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, risk_levels: RiskLevels, trail_state: TrailState,
              on_trail_exit=None) -> None:
        """Called by main.py after a confirmed entry fill."""
        self.risk  = risk_levels
        self.state = trail_state
        self.on_trail_exit = on_trail_exit  # FIX-EXIT: callback to main.py
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
        try:
            await self._loop_rest()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TrailMonitor crashed: {e}", exc_info=True)

    async def _loop_rest(self) -> None:
        _base_url = ("https://testnet-api.india.delta.exchange"
                     if DELTA_TESTNET else
                     "https://api.india.delta.exchange")
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls": {
                "api": {
                    "public" : _base_url,
                    "private": _base_url,
                }
            },
        }
        self._exchange = ccxt.delta(params)
        logger.info(f"Trail ticker polling every {TRAIL_LOOP_SEC}s for {SYMBOL}")

        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    mark_price = float(
                        ticker.get("info", {}).get("mark_price") or
                        ticker.get("last") or 0
                    )
                    if mark_price > 0:
                        await self._on_tick(mark_price)
                    else:
                        logger.warning("Ticker returned zero price — skipping tick")
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
        Process one price tick — matches Pine Script exit priority exactly.

        Pine Script strategy.exit() evaluation order:
          1. stop= (SL)    → checked first
          2. limit= (TP)   → checked second
          3. trail_points= → trailing stop, tightens SL
        
        We replicate:
          1. Max SL        → hard cap, market close
          2. Trail SL cross → current_sl breached, market close  ← FIX-EXIT
          3. Breakeven     → move current_sl to entry (one-shot)
          4. Trail ratchet → advance current_sl toward peak
        """
        if not self._running or self.risk is None or self.state is None:
            return

        risk  = self.risk
        state = self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr

        # ── 1. Max SL ─────────────────────────────────────────────────────────
        if not state.max_sl_fired and max_sl_hit(current_price, entry_price, atr, is_long):
            logger.warning(
                f"MAX SL HIT | price={current_price:.2f} entry={entry_price:.2f}"
            )
            state.max_sl_fired = True
            self._running = False
            await self.telegram.notify_max_sl(current_price, entry_price)
            try:
                exit_order = await self.order_mgr.close_position(reason="Max SL Hit")
                exit_price = float(exit_order.get("average") or exit_order.get("price") or current_price)
            except Exception as e:
                logger.error(f"Emergency close failed: {e}", exc_info=True)
                exit_price = current_price
            if self.on_trail_exit:
                await self.on_trail_exit(exit_price, "Max SL Hit")
            return

        # ── Update peak price ─────────────────────────────────────────────────
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        profit_dist = abs(state.peak_price - entry_price)

        # ── 2. FIX-EXIT: Trail SL breach → market close ───────────────────────
        # Mirrors Pine: strategy.exit() closes when price crosses trailing stop.
        # Only active once trail or BE has moved current_sl off initial SL.
        sl_is_active = state.be_done or state.stage > 0
        if sl_is_active and state.current_sl > 0:
            sl_breached = (
                (is_long  and current_price <= state.current_sl) or
                (not is_long and current_price >= state.current_sl)
            )
            if sl_breached:
                logger.info(
                    f"TRAIL SL BREACHED | price={current_price:.2f} "
                    f"sl={state.current_sl:.2f} stage={state.stage}"
                )
                self._running = False
                reason = f"Trail S{state.stage}" if state.stage > 0 else "Breakeven SL"
                try:
                    exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
                    exit_price = float(
                        exit_order.get("average") or
                        exit_order.get("price") or
                        current_price
                    )
                except Exception as e:
                    logger.error(f"Trail SL close failed: {e}", exc_info=True)
                    exit_price = current_price
                real_pl = calc_real_pl(entry_price, exit_price, is_long, ALERT_QTY)
                await self.telegram.notify_exit(reason, entry_price, exit_price, real_pl)
                if self.on_trail_exit:
                    await self.on_trail_exit(exit_price, reason)
                return

        # ── 3. Breakeven ──────────────────────────────────────────────────────
        if not state.be_done and should_trigger_be(profit_dist, atr):
            logger.info(
                f"BREAKEVEN triggered | profit={profit_dist:.2f} "
                f"threshold={atr * BE_MULT:.2f} | SL → entry={entry_price:.2f}"
            )
            state.be_done    = True
            state.current_sl = entry_price
            await self.telegram.notify_breakeven(entry_price)
            try:
                from config import BRACKET_SL_BUFFER
                await self.order_mgr.modify_sl(entry_price, BRACKET_SL_BUFFER)
            except Exception as e:
                logger.error(f"Breakeven SL modify failed: {e}", exc_info=True)

        # ── 4. Trail ratchet ──────────────────────────────────────────────────
        new_stage = calc_trail_stage(profit_dist, atr)
        if new_stage > state.stage:
            logger.info(f"TRAIL stage {state.stage} → {new_stage}")
            await self.telegram.notify_trail_stage(
                state.stage, new_stage, current_price, state.current_sl
            )
            state.stage = new_stage

        if state.stage > 0:
            trail_pts, trail_off = get_trail_params(state.stage, atr)

            # FIX — remove old `profit_dist >= trail_pts` gate.
            #
            # PineScript places the trail stop at `peak - trail_points` the
            # moment the trail activates (profit >= trail_offset = 0.55 ATR).
            # There is NO second requirement that profit also exceed trail_pts.
            # The old gate caused the bot to skip trail updates when
            # 0.55*ATR <= profit < 0.70*ATR — a range where Pine was already
            # trailing and would close the trade on a reversal.
            #
            # Correct behaviour: once stage > 0, always set SL = peak - trail_pts.
            if is_long:
                candidate_sl = state.peak_price - trail_pts
            else:
                candidate_sl = state.peak_price + trail_pts

            if self._sl_improved(candidate_sl):
                logger.info(
                    f"TRAIL SL update | stage={state.stage} "
                    f"peak={state.peak_price:.2f} pts={trail_pts:.2f} "
                    f"new_sl={candidate_sl:.2f}"
                )
                state.current_sl = candidate_sl
                try:
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
        if self.risk is None or self.state is None:
            return False
        if self.risk.is_long:
            return new_sl > self.state.current_sl
        else:
            return new_sl < self.state.current_sl
