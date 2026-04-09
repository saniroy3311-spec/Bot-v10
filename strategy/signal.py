"""
strategy/signal.py
Entry + Exit signal engine — exact replica of Shiva Sniper v6.5 Pine Script logic.

FIXES vs previous version:
───────────────────────────────────────────────────────────────────────────────
FIX-S1 | Trend Long EMA condition was wrong.
          Pine:  emaFast > emaTrend  (EMA crossover — fast above slow)
          Old:   close > ema_trend AND close > ema_fast  (price vs both EMAs)
          Fixed: snap.ema_fast > snap.ema_trend

FIX-S2 | Trend Long breakout condition was missing.
          Pine:  close > high[1]  (close breaks above prior bar high)
          Old:   not present
          Fixed: snap.close > snap.prev_high

FIX-S3 | Trend Short EMA condition was wrong (same mirror bug as S1).
          Pine:  emaFast < emaTrend
          Old:   close < ema_trend AND close < ema_fast
          Fixed: snap.ema_fast < snap.ema_trend

FIX-S4 | Trend Short breakdown condition was missing.
          Pine:  close < low[1]
          Old:   not present
          Fixed: snap.close < snap.prev_low

Range signals (rangeLong, rangeShort) were already correct — no changes.

FIX-EXIT | Bar-close exit signals NEUTERED.
           Pine Script calls strategy.exit() which manages bracket orders (TP/SL/Trail).
           It does NOT evaluate indicator flips to close positions. 
           evaluate_exit() now returns _NO_EXIT to force the bot to rely purely 
           on trail_loop.py for exits, matching TradingView exactly.
───────────────────────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
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
    regime:      str   # "trend" | "range" | "none"

    @property
    def is_trend(self) -> bool:
        return self.regime == "trend"


_NO_SIGNAL = Signal(signal_type=SignalType.NONE, is_long=False, regime="none")


def evaluate(snap: IndicatorSnapshot, has_position: bool) -> Signal:
    """
    Evaluate entry conditions on a confirmed IndicatorSnapshot.

    Args:
        snap:         Indicator values for the last confirmed bar.
        has_position: True if bot is already in a trade → always returns NONE.

    Returns:
        Signal — contains signal_type, is_long, regime.
    """
    if has_position:
        return _NO_SIGNAL

    if not snap.filters_ok:
        return _NO_SIGNAL

    # ── TREND SIGNALS ─────────────────────────────────────────────────────────
    if snap.trend_regime:

        # Trend Long:
        if (snap.ema_fast > snap.ema_trend
                and snap.dip > snap.dim
                and snap.close > snap.prev_high):
            return Signal(
                signal_type=SignalType.TREND_LONG,
                is_long=True,
                regime="trend",
            )

        # Trend Short:
        if (snap.ema_fast < snap.ema_trend
                and snap.dim > snap.dip
                and snap.close < snap.prev_low):
            return Signal(
                signal_type=SignalType.TREND_SHORT,
                is_long=False,
                regime="trend",
            )

    # ── RANGE SIGNALS ─────────────────────────────────────────────────────────
    if snap.range_regime:

        # Range Long: oversold RSI reversal
        if snap.rsi < RSI_OS:
            return Signal(
                signal_type=SignalType.RANGE_LONG,
                is_long=True,
                regime="range",
            )

        # Range Short: overbought RSI reversal
        if snap.rsi > RSI_OB:
            return Signal(
                signal_type=SignalType.RANGE_SHORT,
                is_long=False,
                regime="range",
            )

    return _NO_SIGNAL


# ── Exit signal ───────────────────────────────────────────────────────────────

@dataclass
class ExitSignal:
    should_exit: bool
    reason: str   # e.g. "Exit TL" / "Exit TS" / "Exit RL" / "Exit RS"


_NO_EXIT = ExitSignal(should_exit=False, reason="")


def evaluate_exit(snap: IndicatorSnapshot, signal_type: SignalType) -> ExitSignal:
    """
    This Pine Script uses strategy.exit() with stop/limit/trail_points/trail_offset ONLY.
    There are NO indicator-flip exits (no strategy.close on EMA/DI/RSI flip).
    All exits are handled by trail_loop.py (TP, SL, Breakeven, Trail, Max SL).

    Returns _NO_EXIT always — do not change this for this strategy.
    """
    return _NO_EXIT
