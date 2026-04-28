"""
execution.py — Shiva Sniper v10  (PARITY-FIX-EXEC-v1)
════════════════════════════════════════════════════════════════════════

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
FIX-EXEC-001 | CRITICAL — Removed frozen-ATR TrailMonitor class.
  The old TrailMonitor here used risk.atr (frozen at fill time) for
  ALL trail calculations (stage triggers, trail distances, breakeven,
  Max SL). Pine uses the CURRENT bar's ATR for all of these.
  This caused trail SL distances to diverge from Pine on any multi-bar
  trade where ATR changed. The correct implementation is in
  monitor/trail_loop.py (FIX-PARITY-01).

  Fix: All TrailMonitor usage now imports from monitor/trail_loop.py.
  The old class is removed entirely. If phase scripts imported it
  from here they must be updated to import from monitor/trail_loop.py.

FIX-EXEC-002 | CRITICAL — Removed tick-level BE and stage upgrade.
  The old TrailMonitor._on_tick() activated breakeven and upgraded
  trail stage on every REST tick. Pine's calc_on_every_tick=false
  means BE and stage upgrade ONLY happen at bar close.
  monitor/trail_loop.py correctly gates these to on_bar_close() only
  (FIX-EXIT-01 / FIX-EXIT-05). Removing the old class eliminates
  this divergence path entirely.

FIX-EXEC-003 | OrderManager re-exported from orders/manager.py.
  Phase scripts that import OrderManager from execution.py now get
  the correct implementation (with FIX-OM-001 through FIX-OM-006)
  instead of the stripped-down legacy version.

WHAT THIS FILE NOW PROVIDES:
──────────────────────────────────────────────────────────────────────
  • Re-exports for backward compat so phase scripts don't break:
      OrderManager       ← orders/manager.py  (canonical, FIX-OM-*)
      TrailMonitor       ← monitor/trail_loop.py (canonical, FIX-PARITY-*)
      build_exchange     ← orders/manager.py
      log_signal()       ← local (unchanged, writes signals.jsonl)
      ExecutionEngine    ← local (thin wrapper around canonical classes)

  • strategy_logic imports re-routed to canonical modules:
      IndicatorSnapshot, Signal, SignalType ← indicators/engine.py
      RiskLevels, TrailState               ← risk/calculator.py
      calc_levels, recalc_levels_from_fill ← risk/calculator.py

  main.py does NOT import from execution.py at all — it imports
  directly from the canonical modules. This file exists only so that
  phase1/phase2/phase3 scripts continue to work without modification.
════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Callable, Optional

# ── Canonical imports (all fixed implementations) ─────────────────────────────

from orders.manager    import OrderManager, build_exchange          # FIX-EXEC-003
from monitor.trail_loop import TrailMonitor                         # FIX-EXEC-001/002
from indicators.engine  import IndicatorSnapshot, Signal, SignalType
from risk.calculator    import (
    RiskLevels, TrailState,
    calc_levels, recalc_levels_from_fill, calc_real_pl,
)
from config import (
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME,
    TRAIL_LOOP_SEC, TRAIL_SL_PRE_FIRE_BUFFER,
)

logger = logging.getLogger("execution")

SIGNAL_LOG_PATH = os.environ.get("SIGNAL_LOG_PATH", "signals.jsonl")


# ── Timeframe helper (unchanged) ──────────────────────────────────────────────

def _timeframe_to_ms(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60 * 1000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    raise ValueError(f"Unknown timeframe: {tf}")


BAR_PERIOD_MS = _timeframe_to_ms(CANDLE_TIMEFRAME)


def candle_boundary(ts_ms: int) -> int:
    return (ts_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS


# ── Signal logger (unchanged) ─────────────────────────────────────────────────

def log_signal(snap: IndicatorSnapshot, sig: Signal, reason: str = "") -> None:
    rec = {
        "timestamp":    snap.timestamp,
        "signal_type":  sig.signal_type.value,
        "is_long":      sig.is_long,
        "is_trend":     sig.is_trend,
        "regime":       sig.regime,
        "close":        snap.close,
        "atr":          snap.atr,
        "adx":          snap.adx,
        "rsi":          snap.rsi,
        "dip":          snap.dip,
        "dim":          snap.dim,
        "filters_ok":   snap.filters_ok,
        "reason":       reason,
    }
    try:
        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        logger.error(f"signal log write failed: {e}")


# ── ExecutionEngine (thin wrapper — canonical classes under the hood) ─────────

class ExecutionEngine:
    """
    Thin orchestration wrapper used by phase scripts and backtest runners.

    FIX-EXEC-001/002: TrailMonitor is now monitor/trail_loop.TrailMonitor
    (live ATR, bar-close-only BE + stage upgrade). The old execution.py
    TrailMonitor with frozen ATR and tick-level BE/stage is removed.

    IMPORTANT: main.py does NOT use ExecutionEngine — it orchestrates
    ShivaSniperBot directly. ExecutionEngine is for phase scripts only.
    """

    def __init__(self) -> None:
        self.order_mgr   = OrderManager()
        self.trail_mon   = TrailMonitor(self.order_mgr, telegram=None, journal=None)
        self.in_position = False
        self.risk        : Optional[RiskLevels] = None
        self.trail_state : Optional[TrailState] = None
        self._signal_type: str = "None"
        self._entry_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self.order_mgr.initialize()

    async def shutdown(self) -> None:
        self.trail_mon.stop()
        await self.order_mgr.close_exchange()

    async def process_closed_bar(
        self,
        snap: IndicatorSnapshot,
        sig:  Signal,
    ) -> None:
        """
        Called once per confirmed bar close by phase runners.
        Mirrors main.py → ShivaSniperBot.on_bar_close() entry path.
        """
        log_signal(snap, sig, reason="bar_close_eval")

        # ── Trail update for open position ────────────────────────────────────
        if self.in_position and self.trail_mon._running:
            self.trail_mon.on_bar_close(
                bar_close   = snap.close,
                bar_high    = snap.high,
                bar_low     = snap.low,
                bar_open    = snap.open,
                current_atr = snap.atr,          # FIX-EXEC-001: live ATR
            )
            return

        # ── Entry evaluation ──────────────────────────────────────────────────
        if self.in_position or sig.signal_type == SignalType.NONE:
            return
        if self._entry_lock.locked():
            return

        async with self._entry_lock:
            if self.in_position:
                return

            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

            try:
                order = await self.order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                )
            except Exception as e:
                logger.error(f"entry order failed: {e}")
                return

            fill = float(order.get("average") or order.get("price") or snap.close)
            risk = recalc_levels_from_fill(risk_pre, fill)

            self.in_position  = True
            self.risk         = risk
            self._signal_type = sig.signal_type.value
            self.trail_state  = TrailState(
                stage      = 0,
                current_sl = risk.sl,
                peak_price = fill,
            )

            self.trail_mon.start(
                risk_levels       = risk,
                trail_state       = self.trail_state,
                entry_bar_time_ms = int(time.time() * 1000),
                on_trail_exit     = self._on_trail_exit,
            )

            log_signal(snap, sig, reason=f"ENTRY_FILL fill={fill:.2f}")
            logger.info(
                f"ENTRY | type={sig.signal_type.value} fill={fill:.2f} "
                f"sl={risk.sl:.2f} tp={risk.tp:.2f} atr={snap.atr:.2f}"
            )

    async def _on_trail_exit(
        self,
        exit_price: float,
        reason: str,
        source: str = "tick",
    ) -> None:
        if not self.in_position:
            return
        self.trail_mon.stop()
        pl = (
            calc_real_pl(self.risk.entry_price, exit_price, self.risk.is_long, ALERT_QTY)
            if self.risk else 0.0
        )
        logger.info(
            f"EXIT | reason={reason} source={source} "
            f"entry={self.risk.entry_price:.2f} exit={exit_price:.2f} pl={pl:+.4f}"
        )
        try:
            with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp":   int(time.time() * 1000),
                    "signal_type": "EXIT",
                    "reason":      reason,
                    "source":      source,
                    "entry_price": self.risk.entry_price if self.risk else None,
                    "exit_price":  exit_price,
                    "pl":          pl,
                }) + "\n")
        except Exception:
            pass
        self.in_position  = False
        self.risk         = None
        self.trail_state  = None
        self._signal_type = "None"
