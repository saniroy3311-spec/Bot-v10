"""
strategy/trend_breakout.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Trend Breakout entry logic (per Trend_Breakout_Strategy_Spec.pdf §2/§6A).

Breakout of the previous bar's high/low inside an ADX-confirmed trend regime,
with EMA-trend alignment and DMI confirmation. Range/RSI-reversal trades are
NEVER taken by this strategy — that is the ONLY entry-side difference from the
live evaluate() in indicators/engine.py, which additionally takes range RSI
trades when range_regime is active.

Activated by setting  ENTRY_STRATEGY=trend_breakout  in .env.
Leave ENTRY_STRATEGY unset (or =rsi_bounce) to keep the current live logic.

Dataclasses (Signal / SignalType / IndicatorSnapshot) are reused from
indicators.engine so calc_levels(), trail_loop and journalling all keep working
unchanged. NOTE: the spec's draft imported these from `strategy_logic`, but that
module is only used by the standalone backtest — main.py's live path goes
through indicators.engine, so we import from there.

SL/TP for this strategy (0.8×ATR, 2.0×R) are applied downstream in
risk/calculator.py::calc_levels() via the TREND_ATR_MULT / TREND_RR globals,
which config.py rebinds to the TB values whenever ENTRY_STRATEGY=trend_breakout.
This module only decides *whether/what* to enter, not the level maths.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from indicators.engine import Signal, SignalType, IndicatorSnapshot
from config import ADX_TREND_TH, BREAKOUT_BUFFER_PTS


def evaluate(snap: IndicatorSnapshot, has_position: bool = False) -> Signal:
    """
    Trend Breakout entry evaluation for a confirmed bar.

    Same signature as indicators.engine.evaluate() so it is a drop-in
    replacement behind the ENTRY_STRATEGY switch in strategy/signal.py:

        sig = evaluate(snap, has_position=False)

    Conditions (mirror the live trend branch, incl. the BREAKOUT_BUFFER_PTS
    feed-divergence guard; buffer defaults to 0 so behaviour matches the spec's
    plain `close > prev_high` when no buffer is configured):

        TREND LONG  : trend_regime and ema_fast > ema_trend and dip > dim
                      and close > prev_high + BREAKOUT_BUFFER_PTS and filters_ok
        TREND SHORT : trend_regime and ema_fast < ema_trend and dim > dip
                      and close < prev_low  - BREAKOUT_BUFFER_PTS and filters_ok

    No range/RSI signals are ever produced. Returns Signal(NONE) if in a
    position, out of trend regime, filters fail, or no breakout.
    """
    if has_position:
        return Signal(SignalType.NONE, False, False, "NONE")

    # Regime gate. snap.trend_regime already encodes (adx > ADX_TREND_TH) with
    # the configured ADX_TOLERANCE; use it directly to stay identical to the
    # live engine. (ADX_TREND_TH imported for clarity / explicit-fallback below.)
    if not snap.trend_regime or not snap.filters_ok:
        return Signal(SignalType.NONE, False, False, "NONE")

    # LONG: breakout above prev bar high, bull structure
    if (snap.close > snap.prev_high + BREAKOUT_BUFFER_PTS
            and snap.ema_fast > snap.ema_trend
            and snap.dip > snap.dim):
        return Signal(SignalType.TREND_LONG, is_long=True, is_trend=True, regime="TREND")

    # SHORT: breakout below prev bar low, bear structure
    if (snap.close < snap.prev_low - BREAKOUT_BUFFER_PTS
            and snap.ema_fast < snap.ema_trend
            and snap.dim > snap.dip):
        return Signal(SignalType.TREND_SHORT, is_long=False, is_trend=True, regime="TREND")

    return Signal(SignalType.NONE, False, False, "NONE")
