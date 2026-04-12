"""
monitor/trail_loop.py - Shiva Sniper v6.5
Tick-level exit monitor — exact replica of Pine Script strategy.exit() mechanic.

═══════════════════════════════════════════════════════════════════
PINE SCRIPT strategy.exit(trail_points, trail_offset) — EXACT SPEC
═══════════════════════════════════════════════════════════════════

  trail_points : profit distance from ENTRY that ACTIVATES trailing
  trail_offset : distance behind PEAK at which the trailing SL sits

  So:
    trail_active  = (peak_price - entry) >= trail_points   [long]
    trailing_sl   = peak_price - trail_offset              [long]
    trailing_sl   = peak_price + trail_offset              [short]

  The trailing SL only ever RATCHETS — it never moves against the trader.

MULTI-STAGE BEHAVIOR:
  Pine code (from strategy):
    activePts = trailStage==5 ? atr*trail5Pts : ... : atr*trail1Pts
    activeOff = trailStage==5 ? atr*trail5Off : ... : atr*trail1Off
    strategy.exit(..., trail_points=activePts, trail_offset=activeOff)

  When trailStage==0 (before any trigger is hit), Pine STILL uses
  trail1Pts / trail1Off — the exit is armed from bar 1.

  trailStage only ever increments. Pine guards: "if trailStage < N and ..."
  So a pullback can NEVER reduce the stage — we use PEAK profit
  for the stage trigger check, not current price.

═══════════════════════════════════════════════════════════════════
BUGS FIXED vs previous version
═══════════════════════════════════════════════════════════════════

FIX-TRAIL-A | Trail SL used pts (activation distance) instead of off (trailing distance).

  Root cause (confirmed from live log):
    Log: "peak=72735.50 pts=232.39 SL 73187.89 → 72967.89"
    72735.50 + 232.39 = 72967.89  ← bot used pts as the SL distance
    Correct: 72735.50 + 182.59 = 72918.09  ← off = 0.55 * ATR

  Fix: trail_sl = peak - active_off  [long]
       trail_sl = peak + active_off  [short]
  where active_off = atr * offset_mult (the TRAILING DISTANCE, e.g. 0.55 ATR).

FIX-TRAIL-B | Stage trigger used current_profit_dist instead of peak_profit_dist.

  Root cause:
    A pullback caused stage to be re-evaluated at a lower current price,
    which could re-trigger stage upgrades from a lower base and produce
    inconsistent SL positions. Pine's "if trailStage < N" guard is
    one-directional — we replicate by using peak_profit_dist.

  Fix: use peak_profit_dist for stage trigger check.

FIX-TRAIL-C | Trail SL was blocked entirely when stage==0 (wrong).

  Root cause:
    "FIX-STAGE-003" in previous version blocked all trail when stage==0.
    But Pine arms trail1Pts/trail1Off from bar 1 — trail CAN activate
    before stage 1 trigger (trail1Pts=0.70 ATR < stage1Trigger=1.0 ATR).

  Fix: always compute trail using current stage params.
       stage==0 → use stage 1 params (index 0), matching Pine's ternary chain.

PRESERVED:
  FIX-TV-001 | Immediate exit — no bar-boundary wait.
  FIX-TV-002 | _exit_triggered guard prevents double-exit.
  FIX-TV-003 | Real wall-clock time in log.
  FIX-007    | Entry-bar same-bar guard (Max SL blocked on entry bar).
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

BAR_PERIOD_MS = 30 * 60 * 1000  # 30 minutes


# ─────────────────────────────────────────────────────────────────────────────
# Pure Pine-parity helpers (stateless — easy to unit-test)
# ─────────────────────────────────────────────────────────────────────────────

def _get_active_params(stage: int, atr: float):
    """
    Return (active_pts, active_off) for the given stage.

    Pine ternary chain: trailStage==5 ? atr*trail5Pts : ... : atr*trail1Pts
    When stage==0, Pine uses trail1Pts/trail1Off (index 0).
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def _upgrade_stage(current_stage: int, peak_profit_dist: float, atr: float) -> int:
    """
    Return the highest stage whose trigger is satisfied.
    Never decrements — mirrors Pine's "if trailStage < N and ..." guards.

    Uses PEAK profit distance so a pullback cannot reset the stage.
    """
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if peak_profit_dist >= atr * trigger_mult:
            new_stage = max(new_stage, i + 1)
            break
    return new_stage


def _compute_trail_sl(
    stage: int,
    peak_price: float,
    peak_profit_dist: float,
    is_long: bool,
    atr: float,
) -> Optional[float]:
    """
    Compute the trailing SL candidate.
    Returns None if trail has not yet activated.

    Pine:
      trail_active = profit_dist >= trail_points  (activation threshold)
      trail_sl     = peak_price  - trail_offset   (long)  ← uses OFFSET, not POINTS
      trail_sl     = peak_price  + trail_offset   (short)
    """
    active_pts, active_off = _get_active_params(stage, atr)
    if peak_profit_dist < active_pts:
        return None                           # not yet active
    return (peak_price - active_off) if is_long else (peak_price + active_off)


# ─────────────────────────────────────────────────────────────────────────────
# TrailMonitor
# ─────────────────────────────────────────────────────────────────────────────

class TrailMonitor:
    def __init__(self, order_manager, telegram, journal):
        self.order_mgr = order_manager
        self.telegram  = telegram
        self.journal   = journal

        self.risk:  Optional[RiskLevels]   = None
        self.state: Optional[TrailState]   = None
        self._running    = False
        self._task: Optional[asyncio.Task] = None
        self._exchange   = None
        self.on_trail_exit: Optional[Callable] = None

        self.entry_bar_time_ms: Optional[int] = None
        self._exit_triggered = False           # FIX-TV-002

        # FIX-EXIT-001: Initial SL uses bar-close price only.
        # Pine Script uses calc_on_every_tick=false — the initial SL is only
        # evaluated at bar close (every 30m). Intrabar wicks that pierce the SL
        # but close back above it do NOT trigger an exit in Pine.
        # _bar_close_price is updated via on_bar_close() from the feed callback.
        # The tick loop checks Initial SL against this price, NOT the live tick.
        self._bar_close_price: Optional[float] = None

    # ── Bar helpers ───────────────────────────────────────────────────────────
    def _is_same_bar(self, timestamp_ms: int) -> bool:
        if self.entry_bar_time_ms is None:
            return False
        return (timestamp_ms // BAR_PERIOD_MS) == (self.entry_bar_time_ms // BAR_PERIOD_MS)

    def on_bar_close(self, bar_close_price: float) -> None:
        """
        FIX-EXIT-001: Called by the feed on every confirmed bar close.
        Updates the bar-close price used for Initial SL evaluation.
        Pine Script: calc_on_every_tick=false → SL only checked at bar close.
        """
        self._bar_close_price = bar_close_price

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def start(
        self,
        risk_levels: RiskLevels,
        trail_state: TrailState,
        entry_bar_time_ms: Optional[int] = None,
        on_trail_exit: Optional[Callable] = None,
    ) -> None:
        self.risk            = risk_levels
        self.state           = trail_state
        self.on_trail_exit   = on_trail_exit
        self._exit_triggered = False
        self.entry_bar_time_ms = entry_bar_time_ms or int(time.time() * 1000)

        if self.state.peak_price == 0.0:
            self.state.peak_price = risk_levels.entry_price

        logger.info(
            f"TrailMonitor started | "
            f"entry={risk_levels.entry_price} atr={risk_levels.atr} "
            f"entry_bar={self.entry_bar_time_ms}"
        )
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("TrailMonitor stopped.")

    async def _run(self) -> None:
        try:
            await self._loop_rest()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TrailMonitor crashed: {e}", exc_info=True)

    async def _loop_rest(self) -> None:
        _base_url = (
            "https://testnet-api.india.delta.exchange"
            if DELTA_TESTNET
            else "https://api.india.delta.exchange"
        )
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
            "urls"           : {"api": {"public": _base_url, "private": _base_url}},
        }
        self._exchange = ccxt.delta(params)
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
                    logger.error(f"Tick processing error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    # ── Core tick — exact Pine strategy.exit() replication ───────────────────
    async def _on_tick(self, current_price: float) -> None:
        if not self._running or self.risk is None or self.state is None:
            return

        risk        = self.risk
        state       = self.state
        is_long     = risk.is_long
        entry_price = risk.entry_price
        atr         = risk.atr
        now_ms      = int(time.time() * 1000)

        # 1. Update peak price ─────────────────────────────────────────────────
        if is_long:
            state.peak_price = max(state.peak_price, current_price)
        else:
            state.peak_price = min(state.peak_price, current_price)

        # 2. Profit distances ──────────────────────────────────────────────────
        # peak_profit_dist    → stage trigger + trail activation (based on best price)
        # current_profit_dist → breakeven check (based on current price, like Pine)
        if is_long:
            peak_profit_dist    = max(0.0, state.peak_price - entry_price)
            current_profit_dist = current_price - entry_price
        else:
            peak_profit_dist    = max(0.0, entry_price - state.peak_price)
            current_profit_dist = entry_price - current_price

        # 3. Target Profit (TP) ────────────────────────────────────────────────
        tp_hit = (is_long and current_price >= risk.tp) or (not is_long and current_price <= risk.tp)
        if tp_hit:
            await self._execute_exit(current_price, "Target Profit")
            return

        # 4. Max SL — blocked on entry bar (FIX-007) ──────────────────────────
        if not self._is_same_bar(now_ms) and not state.max_sl_fired:
            if max_sl_hit(current_price, entry_price, atr, is_long):
                state.max_sl_fired = True
                await self._execute_exit(current_price, "Max SL Hit")
                return

        # 5. Breakeven ─────────────────────────────────────────────────────────
        # Pine: "if not beDone and close - entryPrice > beTrigger"
        # Uses current_profit_dist (current tick price, not peak)
        if not state.be_done and current_profit_dist > atr * BE_MULT:
            state.be_done = True
            be_improves = (
                (is_long  and entry_price > state.current_sl)
                or
                (not is_long and entry_price < state.current_sl)
            )
            if be_improves:
                old_sl = state.current_sl
                state.current_sl = entry_price
                logger.info(f"Breakeven | SL {old_sl:.2f} → {entry_price:.2f}")
                await self.telegram.notify_breakeven(entry_price)

        # 6. Stage upgrade (FIX-TRAIL-B) ──────────────────────────────────────
        # Uses PEAK profit — stage never decrements on a pullback.
        new_stage = _upgrade_stage(state.stage, peak_profit_dist, atr)
        if new_stage > state.stage:
            old_stage   = state.stage
            state.stage = new_stage
            active_pts, active_off = _get_active_params(state.stage, atr)
            logger.info(
                f"TRAIL stage {old_stage} → {state.stage} | "
                f"price={current_price:.2f} peak_profit={peak_profit_dist:.2f} "
                f"pts={active_pts:.2f} off={active_off:.2f}"
            )
            await self.telegram.notify_trail_stage(
                old_stage, state.stage, current_price, state.current_sl
            )

        # 7. Trailing SL (FIX-TRAIL-A + FIX-TRAIL-C) ─────────────────────────
        # Pine arms trail from bar 1 using trail1Pts/trail1Off (stage 0 → index 0).
        # trail_sl = peak ± active_OFF  (the OFFSET, NOT the activation pts)
        # Ratchet: only update if SL improves for the trader.
        trail_sl = _compute_trail_sl(
            state.stage, state.peak_price, peak_profit_dist, is_long, atr
        )
        if trail_sl is not None:
            improves = (
                (is_long  and trail_sl > state.current_sl)
                or
                (not is_long and trail_sl < state.current_sl)
            )
            if improves:
                _, active_off = _get_active_params(state.stage, atr)
                old_sl = state.current_sl
                state.current_sl = trail_sl
                logger.info(
                    f"TRAIL SL update | stage={state.stage} "
                    f"peak={state.peak_price:.2f} off={active_off:.2f} "
                    f"SL {old_sl:.2f} → {trail_sl:.2f}"
                )

        # 8. SL check — split by type to match Pine's calc_on_every_tick=false
        #
        # FIX-EXIT-001: Pine Script (calc_on_every_tick=false) only evaluates the
        # Initial SL at bar CLOSE — intrabar wicks that exceed the SL but close
        # back inside are ignored. The bot's tick loop was firing Initial SL on
        # intrabar price action that Pine never saw, causing early exits.
        #
        # Resolution:
        #   Initial SL  → use _bar_close_price (updated only on confirmed bars)
        #   Breakeven SL / Trail SL → use current tick price (protecting gains is OK tick-by-tick)
        #   TP             → use current tick price (take profit immediately)
        #   Max SL         → use current tick price (hard gap-protection, safety net only)
        #
        if state.current_sl > 0:
            trail_active = (
                (is_long  and state.current_sl > risk.sl)
                or
                (not is_long and state.current_sl < risk.sl)
            )

            if trail_active:
                # Trail SL or Breakeven SL — evaluate on every tick (protecting gains)
                sl_hit = (
                    (is_long  and current_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER)
                    or
                    (not is_long and current_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
                )
                if sl_hit:
                    be_at_entry = (
                        state.be_done
                        and abs(state.current_sl - entry_price) <= max(1e-9, atr * 1e-6)
                    )
                    reason = "Breakeven SL" if be_at_entry else f"Trail S{state.stage}"
                    await self._execute_exit(current_price, reason)
                    return
            else:
                # Initial SL — only evaluate at bar close (Pine parity: calc_on_every_tick=false)
                check_price = self._bar_close_price
                if check_price is not None:
                    sl_hit = (
                        (is_long  and check_price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER)
                        or
                        (not is_long and check_price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
                    )
                    if sl_hit:
                        logger.info(
                            f"Initial SL hit at bar close | bar_close={check_price:.2f} "
                            f"sl={state.current_sl:.2f}"
                        )
                        await self._execute_exit(check_price, "Initial SL")
                        return

    # ── FIX-TV-001: Immediate exit — no bar-boundary wait ────────────────────
    async def _execute_exit(self, price: float, reason: str) -> None:
        if self._exit_triggered:               # FIX-TV-002: double-exit guard
            return
        self._exit_triggered = True
        self._running = False

        try:
            exit_order = await self.order_mgr.close_at_trail_sl(reason=reason)
            fill_price = float(
                exit_order.get("average")
                or exit_order.get("price")
                or price
            )
        except Exception as e:
            logger.error(f"Exit order failed: {e}")
            fill_price = price

        real_pl = calc_real_pl(
            self.risk.entry_price, fill_price, self.risk.is_long, ALERT_QTY
        )
        wall_time = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        logger.info(
            f"Position closed | exit={fill_price:.2f} reason={reason} "
            f"pl={real_pl:.4f} time={wall_time}"
        )
        try:
            await self.telegram.notify_exit(
                reason, self.risk.entry_price, fill_price, real_pl
            )
        except Exception as tg_err:
            logger.error(f"Telegram notify_exit failed (non-fatal): {tg_err}")

        if self.on_trail_exit:
            await self.on_trail_exit(fill_price, reason)
