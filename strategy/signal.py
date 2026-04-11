"""
strategy/signal.py — Shiva Sniper v6.5-30M
Exact replica of Pine Script entry conditions.

═══════════════════════════════════════════════════════════════════
PINE SCRIPT ENTRY CONDITIONS (verbatim source):
═══════════════════════════════════════════════════════════════════

  trendLong  = trendRegime and emaFast > emaTrend and dip > dim
               and close > high[1] and filters
  trendShort = trendRegime and emaFast < emaTrend and dim > dip
               and close < low[1] and filters
  rangeLong  = rangeRegime and rsi < rsiOS and filters
  rangeShort = rangeRegime and rsi > rsiOB and filters

  filters = atr < ta.sma(atr, 50) * filterATRmul
            and volume > ta.sma(volume, 20)
            and math.abs(close - open) > atr * filterBody

  trendRegime = adx > adxTrendTh   (20)
  rangeRegime = adx < adxRangeTh   (18)

═══════════════════════════════════════════════════════════════════
PINE SCRIPT EXIT CONDITIONS:
═══════════════════════════════════════════════════════════════════

  Pine exits ONLY via strategy.exit() — SL / TP / Trail.
  There is NO bar-close signal-reversal exit in Pine.
  evaluate_exit() therefore always returns should_exit=False.
  All real exits are handled by trail_loop.py (tick resolution).

═══════════════════════════════════════════════════════════════════
BUG HISTORY:
═══════════════════════════════════════════════════════════════════
  BUG-SIG-001 | evaluate_exit() previously closed positions when
               entry conditions reversed at bar close. Pine has no
               such logic — exits are purely SL/TP/trail-based.
               FIX: evaluate_exit always returns should_exit=False.
               Exits managed entirely in trail_loop.py.
"""

from enum import Enum
from dataclasses import dataclass
from indicators.engine import IndicatorSnapshot
from config import RSI_OB, RSI_OS


class SignalType(Enum):
    NONE        = "None"
    TREND_LONG  = "Trend Long"
    TREND_SHORT = "Trend Short"
    RANGE_LONG  = "Range Long"
    RANGE_SHORT = "Range Short"


@dataclass
class Signal:
    signal_type: SignalType
    is_long:     bool
    is_trend:    bool
    regime:      str        # "trend" | "range" | "none"


@dataclass
class ExitSignal:
    """
    Pine Script has NO bar-close signal-reversal exit.
    should_exit is always False — exits are handled by trail_loop.py.
    """
    should_exit: bool = False
    reason:      str  = ""


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY EVALUATION — exact Pine parity
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(snap: IndicatorSnapshot, has_position: bool) -> Signal:
    """
    Evaluate entry conditions for the confirmed bar.

    Maps 1:1 to Pine Script:
      trendLong  = trendRegime and emaFast > emaTrend and dip > dim
                   and close > high[1] and filters
      trendShort = trendRegime and emaFast < emaTrend and dim > dip
                   and close < low[1]  and filters
      rangeLong  = rangeRegime and rsi < rsiOS and filters
      rangeShort = rangeRegime and rsi > rsiOB and filters

    Args:
        snap:         IndicatorSnapshot for the just-closed bar.
        has_position: True if a position is currently open.

    Returns:
        Signal with signal_type=NONE if no entry, or the appropriate type.
    """
    if has_position:
        return Signal(SignalType.NONE, False, False, "none")

    f   = snap.filters_ok
    tr  = snap.trend_regime
    rr  = snap.range_regime

    # Pine: close > high[1]  →  snap.close > snap.prev_high
    # Pine: close < low[1]   →  snap.close < snap.prev_low
    trend_long = (
        tr
        and snap.ema_fast > snap.ema_trend
        and snap.dip > snap.dim
        and snap.close > snap.prev_high
        and f
    )
    trend_short = (
        tr
        and snap.ema_fast < snap.ema_trend
        and snap.dim > snap.dip
        and snap.close < snap.prev_low
        and f
    )
    range_long  = rr and snap.rsi < RSI_OS and f
    range_short = rr and snap.rsi > RSI_OB and f

    # Priority order matches Pine: trendLong → trendShort → rangeLong → rangeShort
    if trend_long:
        return Signal(SignalType.TREND_LONG,  is_long=True,  is_trend=True,  regime="trend")
    if trend_short:
        return Signal(SignalType.TREND_SHORT, is_long=False, is_trend=True,  regime="trend")
    if range_long:
        return Signal(SignalType.RANGE_LONG,  is_long=True,  is_trend=False, regime="range")
    if range_short:
        return Signal(SignalType.RANGE_SHORT, is_long=False, is_trend=False, regime="range")

    return Signal(SignalType.NONE, False, False, "none")


# ─────────────────────────────────────────────────────────────────────────────
# EXIT EVALUATION — Pine has NO bar-close exit
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_exit(snap: IndicatorSnapshot, current_signal_type: SignalType) -> ExitSignal:
    """
    Pine Script exits ONLY via strategy.exit() (SL / TP / Trail).
    There is NO bar-close signal-reversal or condition-reversal exit.

    This function intentionally returns should_exit=False always.
    All real exits are handled at tick resolution in trail_loop.py.

    BUG-SIG-001 FIX: previous version closed trades here when entry
    conditions reversed — that behaviour has NO equivalent in Pine Script
    and caused massive exit timing divergence vs TradingView.
    """
    return ExitSignal(should_exit=False, reason="")
