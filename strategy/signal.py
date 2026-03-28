"""
strategy/signal.py
Entry signal engine — exact replica of Shiva Sniper v6.5 Pine Script logic.

Signal priority (mirrors Pine):
  1. Trend Long  — ADX trending, price > EMA200, close > EMA50, +DI > -DI, filters pass, RSI not OB
  2. Trend Short — ADX trending, price < EMA200, close < EMA50, -DI > +DI, filters pass, RSI not OS
  3. Range Long  — ADX ranging, +DI > -DI, RSI oversold, filters pass
  4. Range Short — ADX ranging, -DI > +DI, RSI overbought, filters pass

No position check: only evaluate when has_position=False.
Returns SignalType.NONE when no conditions are met.
"""

from dataclasses import dataclass
from enum import Enum
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
        # Trend Long: price above both EMAs, momentum aligned (+DI > -DI), not overbought
        if (snap.close > snap.ema_trend
                and snap.close > snap.ema_fast
                and snap.dip > snap.dim
                and snap.rsi < RSI_OB):
            return Signal(
                signal_type=SignalType.TREND_LONG,
                is_long=True,
                regime="trend",
            )

        # Trend Short: price below both EMAs, momentum aligned (-DI > +DI), not oversold
        if (snap.close < snap.ema_trend
                and snap.close < snap.ema_fast
                and snap.dim > snap.dip
                and snap.rsi > RSI_OS):
            return Signal(
                signal_type=SignalType.TREND_SHORT,
                is_long=False,
                regime="trend",
            )

    # ── RANGE SIGNALS ─────────────────────────────────────────────────────────
    if snap.range_regime:
        # Range Long: oversold reversal, momentum shows buyers (+DI > -DI)
        if snap.dip > snap.dim and snap.rsi < RSI_OS:
            return Signal(
                signal_type=SignalType.RANGE_LONG,
                is_long=True,
                regime="range",
            )

        # Range Short: overbought reversal, momentum shows sellers (-DI > +DI)
        if snap.dim > snap.dip and snap.rsi > RSI_OB:
            return Signal(
                signal_type=SignalType.RANGE_SHORT,
                is_long=False,
                regime="range",
            )

    return _NO_SIGNAL
