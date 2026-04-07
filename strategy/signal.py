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

FIX-EXIT | Bar-close exit signals added (evaluate_exit).
           ROOT CAUSE of TV vs bot divergence:
           Pine Script calls strategy.exit("Exit TL", "Trend Long") which
           fires at bar close when the TREND LONG conditions are no longer
           valid — specifically when the EMA alignment flips (emaFast <
           emaTrend) OR the DI cross flips (dim > dip). The bot had NO
           bar-close exit evaluation at all — it only exited via the trail
           tick-loop (seconds resolution) or bracket orders. This caused
           bot positions to be held for hours while Pine had already exited
           on the bar close.

           TV Trade #1211 (Apr 7 00:00):
             Entry 69,822.5 → Pine exit 69,884.8 (+0.062 USD) via "Exit TL"
             Bot entry 69,823.5 → bot exit 69,127.5 via Max SL Hit (−0.011)

           evaluate_exit() now returns ExitSignal with the reason string when
           the position should be closed at bar close, exactly mirroring the
           Pine strategy.exit() condition for each signal type.
───────────────────────────────────────────────────────────────────────────────

Signal priority (mirrors Pine exactly):
  1. Trend Long  — ADX trending, emaFast > emaTrend, close > prev_high,
                   +DI > -DI, filters pass
  2. Trend Short — ADX trending, emaFast < emaTrend, close < prev_low,
                   -DI > +DI, filters pass
  3. Range Long  — ADX ranging, RSI oversold, filters pass
  4. Range Short — ADX ranging, RSI overbought, filters pass

Exit evaluation (called every bar close when in_position=True):
  Trend Long  → exit when emaFast < emaTrend  OR  dim > dip  (trend flipped)
  Trend Short → exit when emaFast > emaTrend  OR  dip > dim  (trend flipped)
  Range Long  → exit when RSI > RSI_OB        (overbought)
  Range Short → exit when RSI < RSI_OS        (oversold)
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


# ── Exit signal ───────────────────────────────────────────────────────────────

@dataclass
class ExitSignal:
    should_exit: bool
    reason: str   # e.g. "Exit TL" / "Exit TS" / "Exit RL" / "Exit RS"


_NO_EXIT = ExitSignal(should_exit=False, reason="")


def evaluate_exit(snap: IndicatorSnapshot, signal_type: SignalType) -> ExitSignal:
    """
    FIX-EXIT: Evaluate bar-close exit conditions for an open position.

    Mirrors Pine Script strategy.exit() which fires when the conditions that
    created the entry are no longer valid.  Called every bar close while
    in_position=True.

    Exit logic per signal type:
    ─────────────────────────────────────────────────────────────────────────
    Trend Long  (exitLong):
        emaFast < emaTrend   → EMA alignment flipped bearish
        OR dim > dip         → momentum / DI cross turned bearish

    Trend Short (exitShort):
        emaFast > emaTrend   → EMA alignment flipped bullish
        OR dip > dim         → momentum / DI cross turned bullish

    Range Long  (exitRangeLong):
        RSI > RSI_OB         → overbought, reversal expected

    Range Short (exitRangeShort):
        RSI < RSI_OS         → oversold, reversal expected
    ─────────────────────────────────────────────────────────────────────────
    """
    if signal_type == SignalType.TREND_LONG:
        if snap.ema_fast < snap.ema_trend or snap.dim > snap.dip:
            return ExitSignal(should_exit=True, reason="Exit TL")

    elif signal_type == SignalType.TREND_SHORT:
        if snap.ema_fast > snap.ema_trend or snap.dip > snap.dim:
            return ExitSignal(should_exit=True, reason="Exit TS")

    elif signal_type == SignalType.RANGE_LONG:
        if snap.rsi > RSI_OB:
            return ExitSignal(should_exit=True, reason="Exit RL")

    elif signal_type == SignalType.RANGE_SHORT:
        if snap.rsi < RSI_OS:
            return ExitSignal(should_exit=True, reason="Exit RS")

    return _NO_EXIT
