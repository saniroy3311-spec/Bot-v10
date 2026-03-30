"""
strategy/signal.py
Entry signal engine — exact replica of Shiva Sniper v6.5 Pine Script logic.

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
───────────────────────────────────────────────────────────────────────────────

Signal priority (mirrors Pine exactly):
  1. Trend Long  — ADX trending, emaFast > emaTrend, close > prev_high,
                   +DI > -DI, filters pass
  2. Trend Short — ADX trending, emaFast < emaTrend, close < prev_low,
                   -DI > +DI, filters pass
  3. Range Long  — ADX ranging, RSI oversold, filters pass
  4. Range Short — ADX ranging, RSI overbought, filters pass

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

        # Trend Long:
        #   Pine: emaFast > emaTrend        ← FIX-S1: EMA crossover not price vs EMA
        #         and dip > dim             ← momentum aligned bullish
        #         and close > high[1]       ← FIX-S2: breakout above prior bar high
        #         and filters               ← ATR / volume / body filters
        if (snap.ema_fast > snap.ema_trend
                and snap.dip > snap.dim
                and snap.close > snap.prev_high):
            return Signal(
                signal_type=SignalType.TREND_LONG,
                is_long=True,
                regime="trend",
            )

        # Trend Short:
        #   Pine: emaFast < emaTrend        ← FIX-S3: EMA crossover
        #         and dim > dip             ← momentum aligned bearish
        #         and close < low[1]        ← FIX-S4: breakdown below prior bar low
        #         and filters
        if (snap.ema_fast < snap.ema_trend
                and snap.dim > snap.dip
                and snap.close < snap.prev_low):
            return Signal(
                signal_type=SignalType.TREND_SHORT,
                is_long=False,
                regime="trend",
            )

    # ── RANGE SIGNALS ─────────────────────────────────────────────────────────
    # These matched Pine already — no changes.
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
