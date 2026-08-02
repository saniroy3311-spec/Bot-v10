"""
strategy package — unified signal interface
══════════════════════════════════════════════════════════════════════════════

Re-exports the active strategy implementation.
Default: RSI Bounce (strategy_logic.py / indicators/engine.py)
Optional: Trend Breakout (strategy_trend_breakout.py) — activated via STRATEGY_MODE

Usage:
    from strategy import evaluate, calc_levels, SignalType, Signal, IndicatorSnapshot

    # For Trend Breakout mode:
    from strategy import evaluate_trend_breakout, calc_levels_tb
"""
# RSI Bounce (live default) — from indicators/engine.py
from indicators.engine import (
    evaluate,
    calc_levels,
    SignalType,
    Signal,
    IndicatorSnapshot,
    compute_full_series,
    compute,
)

# Trend Breakout (isolated mode) — from strategy_trend_breakout.py
from .strategy_trend_breakout import (
    evaluate_trend_breakout,
    calc_levels_tb,
    compute_full_series_tb,
    compute_tb,
)

__all__ = [
    # RSI Bounce (default)
    "evaluate",
    "calc_levels",
    "SignalType",
    "Signal",
    "IndicatorSnapshot",
    "compute_full_series",
    "compute",
    # Trend Breakout (opt-in)
    "evaluate_trend_breakout",
    "calc_levels_tb",
    "compute_full_series_tb",
    "compute_tb",
]