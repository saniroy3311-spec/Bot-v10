"""
strategy/signal.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Thin re-export / selection layer so main.py can import from a clean 'strategy'
namespace:

    from strategy.signal import evaluate, SignalType

Two entry strategies are selectable via the ENTRY_STRATEGY env var (see
config.py):

    ENTRY_STRATEGY unset / "rsi_bounce"  (DEFAULT)
        -> evaluate = indicators.engine.evaluate
        The existing live logic: trend breakout in trend regime PLUS RSI
        reversal trades in range regime. Behaviour is byte-identical to before
        this switch existed.

    ENTRY_STRATEGY = "trend_breakout"
        -> evaluate = strategy.trend_breakout.evaluate
        Trend-breakout only; range/RSI trades suppressed. SL/TP shift to
        0.8×ATR / 2.0×R because config.py rebinds TREND_ATR_MULT / TREND_RR
        to the TB values in this mode (read live by risk/calculator.py and
        monitor/trail_loop.py — no edits needed there).

SignalType / Signal / IndicatorSnapshot are always re-exported from
indicators.engine so every downstream consumer (calc_levels, trail_loop,
journal) is unaffected by the switch.
══════════════════════════════════════════════════════════════════════════════
"""

# Dataclasses + the default engine evaluate are always available.
from indicators.engine import (   # noqa: F401  (re-exports)
    evaluate as _evaluate_rsi_bounce,
    SignalType,
    Signal,
    IndicatorSnapshot,
)

from config import ENTRY_STRATEGY

if ENTRY_STRATEGY == "trend_breakout":
    from strategy.trend_breakout import evaluate as evaluate   # noqa: F401
else:
    evaluate = _evaluate_rsi_bounce   # noqa: F401  (existing live logic)
