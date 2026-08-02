"""
infra/shadow_logger.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Per-bar "shadow" decision logger (Trend_Breakout_Strategy_Spec.pdf §6C).

Purpose: while the bot runs (paper OR live), append one structured JSONL row per
confirmed bar capturing exactly what the strategy decided and the levels it
*intended* to use. That log is then compared against the backtest with
phase2/shadow_compare.py to detect divergence between live behaviour and the
backtest that justified the strategy — before scaling real size.

Design rules:
  • OPT-IN. Does nothing unless SHADOW_LOG_ENABLED=true in .env. The live loop
    is byte-for-byte unaffected when it's off.
  • NEVER raises. Every public call is wrapped so a logging fault can never
    interrupt trading. Failures are swallowed (logged once at WARNING).
  • NO heavy deps. Pure stdlib (json/os/time) + a lazy calc_levels import.
  • Append-only JSONL — one JSON object per line, safe to tail live.

Env:
  SHADOW_LOG_ENABLED   "true"/"false"   (default false)
  SHADOW_LOG_PATH      path to .jsonl   (default "shadow_log.jsonl" in cwd)
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("SHADOW_LOG_ENABLED", "false").strip().lower() == "true"
_PATH    = os.environ.get("SHADOW_LOG_PATH", "shadow_log.jsonl")
_warned  = False   # so we don't spam the log if writes keep failing


def is_enabled() -> bool:
    return _ENABLED


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        logger.warning("shadow_logger disabled after error: %s", msg)
        _warned = True


def _intended_levels(snap: Any, sig: Any) -> Optional[dict]:
    """
    Compute the SL/TP the bot WOULD place for this signal, using the exact same
    calc_levels() the live path uses (so it reflects ENTRY_STRATEGY's 0.8/2.0
    when trend_breakout is active). Returns None on NONE signals or any fault.
    """
    try:
        from strategy.signal import SignalType
        if sig is None or sig.signal_type == SignalType.NONE:
            return None
        from risk.calculator import calc_levels
        r = calc_levels(
            entry_price  = float(snap.close),
            atr          = float(snap.atr),
            is_long      = bool(sig.is_long),
            is_trend     = bool(sig.is_trend),
            signal_close = float(snap.close),
        )
        return {
            "intended_entry": round(float(snap.close), 2),
            "intended_sl":    round(float(r.sl), 2),
            "intended_tp":    round(float(r.tp), 2),
            "stop_dist":      round(float(r.stop_dist), 2),
        }
    except Exception as exc:   # never let level calc break logging
        _warn_once(f"level calc: {exc!r}")
        return None


def log_bar(snap: Any, candidate_sig: Any, in_position: bool,
            entry_strategy: Optional[str] = None) -> None:
    """
    Log one confirmed bar. Call once per bar close, right after the strategy's
    evaluate() has produced the candidate signal. No-op unless enabled.

    snap           : IndicatorSnapshot for the closed bar
    candidate_sig  : Signal returned by evaluate(snap, has_position=False)
    in_position    : whether the bot currently holds a position
    entry_strategy : optional label ("rsi_bounce"/"trend_breakout") for the row
    """
    if not _ENABLED:
        return
    try:
        sig_type = getattr(getattr(candidate_sig, "signal_type", None), "value", "None")
        is_long  = getattr(candidate_sig, "is_long", None)
        regime   = getattr(candidate_sig, "regime", "NONE")
        fired    = sig_type != "None"

        if entry_strategy is None:
            try:
                from config import ENTRY_STRATEGY as _es
                entry_strategy = _es
            except Exception:
                entry_strategy = "unknown"

        row: dict[str, Any] = {
            "event":        "bar",
            "logged_at":    int(time.time() * 1000),
            "ts":           int(getattr(snap, "timestamp", 0)),
            "strategy":     entry_strategy,
            "in_position":  bool(in_position),
            # regime + indicators (§6C)
            "regime":       regime,
            "trend_regime": bool(getattr(snap, "trend_regime", False)),
            "range_regime": bool(getattr(snap, "range_regime", False)),
            "adx":          round(float(getattr(snap, "adx", 0.0)), 2),
            "ema_trend":    round(float(getattr(snap, "ema_trend", 0.0)), 2),
            "ema_fast":     round(float(getattr(snap, "ema_fast", 0.0)), 2),
            "dip":          round(float(getattr(snap, "dip", 0.0)), 2),
            "dim":          round(float(getattr(snap, "dim", 0.0)), 2),
            "rsi":          round(float(getattr(snap, "rsi", 0.0)), 2),
            "atr":          round(float(getattr(snap, "atr", 0.0)), 2),
            "close":        round(float(getattr(snap, "close", 0.0)), 2),
            "prev_high":    round(float(getattr(snap, "prev_high", 0.0)), 2),
            "prev_low":     round(float(getattr(snap, "prev_low", 0.0)), 2),
            "filters_ok":   bool(getattr(snap, "filters_ok", False)),
            # signal
            "signal":       sig_type,
            "is_long":      (None if is_long is None else bool(is_long)),
            "fired":        fired,
            # would this actually be taken? (fired AND flat)
            "actionable":   bool(fired and not in_position),
        }
        levels = _intended_levels(snap, candidate_sig) if fired else None
        if levels:
            row.update(levels)

        _append(row)
    except Exception as exc:
        _warn_once(repr(exc))


def log_event(event: str, **fields: Any) -> None:
    """
    Log an arbitrary lifecycle event (e.g. an actual entry/exit with realized
    P/L) for shadow accounting. No-op unless enabled. Never raises.
    """
    if not _ENABLED:
        return
    try:
        row = {"event": event, "logged_at": int(time.time() * 1000)}
        row.update(fields)
        _append(row)
    except Exception as exc:
        _warn_once(repr(exc))


def _append(row: dict) -> None:
    with open(_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
