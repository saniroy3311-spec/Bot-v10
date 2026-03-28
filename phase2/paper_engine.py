"""
phase2/paper_engine.py
Paper trading engine — simulates Shiva Sniper v6.5 on historical OHLCV.

Mirrors Pine Script execution model EXACTLY:
  - Processes bars in order (no lookahead)
  - Entry fires on confirmed bar close (bar N)
  - Exit evaluated on bars N+1, N+2, ... (no same-bar exit)
  - 5-stage trail ratchet per bar
  - Breakeven move
  - Max SL guard

Output: list of PaperTrade — every entry/exit with bar index,
        price, P/L, exit reason, trail stage at exit.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from indicators.engine  import compute_full_series
from strategy.signal    import evaluate, SignalType, Signal
from risk.calculator    import (
    calc_levels, calc_trail_stage, get_trail_params,
    should_trigger_be, max_sl_hit, calc_real_pl, RiskLevels,
)
from config import ALERT_QTY, COMMISSION_PCT


@dataclass
class PaperTrade:
    """One complete trade record."""
    trade_id:     int
    signal_type:  str
    is_long:      bool
    entry_bar:    int        # bar_index at entry
    entry_ts:     int        # timestamp at entry
    entry_price:  float
    sl:           float
    tp:           float
    stop_dist:    float
    atr_at_entry: float
    exit_bar:     int        = 0
    exit_ts:      int        = 0
    exit_price:   float      = 0.0
    exit_reason:  str        = ""
    trail_stage:  int        = 0
    real_pl:      float      = 0.0
    bars_held:    int        = 0


@dataclass
class EngineState:
    """Mutable state during paper trading."""
    in_position:  bool           = False
    trade:        Optional[PaperTrade] = None
    current_sl:   float          = 0.0
    peak_price:   float          = 0.0   # FIX: track peak for trail anchor
    be_done:      bool           = False
    trail_stage:  int            = 0
    max_sl_fired: bool           = False


def run(df: pd.DataFrame) -> list[PaperTrade]:
    """
    Run paper trading on full OHLCV DataFrame.

    Args:
        df: Raw OHLCV with columns [timestamp, open, high, low, close, volume]
            Needs ≥ EMA_TREND_LEN + 10 rows.

    Returns:
        List of completed PaperTrade objects.
    """
    # Compute full indicator series (all bars at once — same as TV)
    series   = compute_full_series(df)
    trades   = []
    state    = EngineState()
    trade_id = 0

    # Start from first bar where all indicators are valid
    for i in range(1, len(series)):
        row      = series.iloc[i]
        prev_row = series.iloc[i - 1]

        ts    = int(row["timestamp"])
        open_ = float(row["open"])
        high  = float(row["high"])
        low   = float(row["low"])
        close = float(row["close"])

        # ── Build mini IndicatorSnapshot from series row ──────────────
        snap = _row_to_snap(row, prev_row)

        # ── OPEN POSITION: evaluate exit conditions ───────────────────
        if state.in_position:
            t = state.trade

            # FIX: Track peak price — Pine anchors trail to highest high (long)
            # or lowest low (short) since entry. Paper engine must mirror this.
            if t.is_long:
                state.peak_price = max(state.peak_price, high)
            else:
                state.peak_price = min(state.peak_price, low)

            peak_price = state.peak_price

            # Profit distance (mirrors Pine profitDist — uses close, not peak)
            profit_dist = (close - t.entry_price) if t.is_long \
                          else (t.entry_price - close)

            # Trail stage ratchet (uses profit_dist from close, mirrors Pine)
            new_stage = calc_trail_stage(profit_dist, t.atr_at_entry)
            if new_stage > state.trail_stage:
                state.trail_stage = new_stage
                t.trail_stage     = new_stage

            # Breakeven
            if not state.be_done and should_trigger_be(profit_dist, t.atr_at_entry):
                state.current_sl = t.entry_price
                state.be_done    = True

            # Trail ratchet SL — FIX: anchor to peak_price, NOT close.
            # Pine: strategy.exit(trail_points=X) -> stop = highest_high - X
            # trail_offset is a bracket-limit buffer, NOT subtracted from SL.
            if state.trail_stage > 0:
                pts, _ = get_trail_params(state.trail_stage, t.atr_at_entry)
                if t.is_long:
                    candidate = peak_price - pts
                    if candidate > state.current_sl:
                        state.current_sl = candidate
                else:
                    candidate = peak_price + pts
                    if candidate < state.current_sl:
                        state.current_sl = candidate

            # ── Check exits (intra-bar using high/low) ────────────────
            exit_price  = None
            exit_reason = None

            if t.is_long:
                # TP hit (high touches limit)
                if high >= t.tp:
                    exit_price  = t.tp
                    exit_reason = "TP"
                # SL hit (low touches stop) — current_sl may have moved
                elif low <= state.current_sl:
                    exit_price  = state.current_sl
                    exit_reason = "SL" if not state.be_done else "Trail/BE SL"
                # Max SL guard
                elif max_sl_hit(low, t.entry_price, t.atr_at_entry, True):
                    exit_price  = t.entry_price - min(
                        t.atr_at_entry * 1.5, 500.0)
                    exit_reason = "Max SL"
            else:
                # TP hit (low touches limit)
                if low <= t.tp:
                    exit_price  = t.tp
                    exit_reason = "TP"
                # SL hit (high touches stop)
                elif high >= state.current_sl:
                    exit_price  = state.current_sl
                    exit_reason = "SL" if not state.be_done else "Trail/BE SL"
                # Max SL guard
                elif max_sl_hit(high, t.entry_price, t.atr_at_entry, False):
                    exit_price  = t.entry_price + min(
                        t.atr_at_entry * 1.5, 500.0)
                    exit_reason = "Max SL"

            if exit_price is not None:
                t.exit_bar    = i
                t.exit_ts     = ts
                t.exit_price  = exit_price
                t.exit_reason = exit_reason
                t.bars_held   = i - t.entry_bar
                t.real_pl     = calc_real_pl(
                    t.entry_price, exit_price, ALERT_QTY, t.is_long
                )
                trades.append(t)
                state = EngineState()   # reset
            continue   # done for this bar if in position

        # ── NO POSITION: evaluate entry ───────────────────────────────
        sig = evaluate(snap, has_position=False)
        if sig.signal_type == SignalType.NONE:
            continue

        risk = calc_levels(
            entry_price = close,
            atr         = float(row["atr"]),
            is_long     = sig.is_long,
            is_trend    = (sig.regime == "trend"),
        )

        trade_id += 1
        t = PaperTrade(
            trade_id     = trade_id,
            signal_type  = sig.signal_type.value,
            is_long      = sig.is_long,
            entry_bar    = i,
            entry_ts     = ts,
            entry_price  = close,
            sl           = risk.sl,
            tp           = risk.tp,
            stop_dist    = risk.stop_dist,
            atr_at_entry = float(row["atr"]),
        )
        state.in_position = True
        state.trade       = t
        state.current_sl  = risk.sl
        state.peak_price  = close    # FIX: init peak to entry price
        state.be_done     = False
        state.trail_stage = 0

    return trades


def trades_to_df(trades: list[PaperTrade]) -> pd.DataFrame:
    """Convert trade list to DataFrame for analysis/export."""
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        rows.append({
            "trade_id"    : t.trade_id,
            "signal_type" : t.signal_type,
            "is_long"     : t.is_long,
            "entry_bar"   : t.entry_bar,
            "entry_ts"    : t.entry_ts,
            "entry_price" : round(t.entry_price, 2),
            "sl"          : round(t.sl, 2),
            "tp"          : round(t.tp, 2),
            "stop_dist"   : round(t.stop_dist, 2),
            "atr"         : round(t.atr_at_entry, 2),
            "exit_bar"    : t.exit_bar,
            "exit_ts"     : t.exit_ts,
            "exit_price"  : round(t.exit_price, 2),
            "exit_reason" : t.exit_reason,
            "trail_stage" : t.trail_stage,
            "bars_held"   : t.bars_held,
            "real_pl"     : round(t.real_pl, 2),
        })
    return pd.DataFrame(rows)


# ── Internal helper ────────────────────────────────────────────────────

class _Snap:
    """Lightweight snap from series row — avoids full recompute."""
    pass


def _row_to_snap(row, prev_row) -> object:
    """Build a minimal IndicatorSnapshot-compatible object from series rows."""
    from indicators.engine import IndicatorSnapshot
    from config import ADX_TREND_TH, ADX_RANGE_TH, FILTER_ATR_MULT, FILTER_BODY_MULT

    atr    = float(row["atr"])
    atr_ok = atr < float(row["atr_sma"]) * FILTER_ATR_MULT
    vol_ok = float(row["volume"]) > float(row["vol_sma"])
    body_ok= abs(float(row["close"]) - float(row["open"])) > atr * FILTER_BODY_MULT

    return IndicatorSnapshot(
        ema_trend    = float(row["ema200"]),
        ema_fast     = float(row["ema50"]),
        atr          = atr,
        rsi          = float(row["rsi"]),
        dip          = float(row["dip"]),
        dim          = float(row["dim"]),
        adx          = float(row["adx"]),
        adx_raw      = float(row["adx_raw"]),
        vol_sma      = float(row["vol_sma"]),
        atr_sma      = float(row["atr_sma"]),
        trend_regime = float(row["adx"]) > ADX_TREND_TH,
        range_regime = float(row["adx"]) < ADX_RANGE_TH,
        filters_ok   = atr_ok and vol_ok and body_ok,
        atr_ok       = atr_ok,
        vol_ok       = vol_ok,
        body_ok      = body_ok,
        open         = float(row["open"]),
        high         = float(row["high"]),
        low          = float(row["low"]),
        close        = float(row["close"]),
        volume       = float(row["volume"]),
        prev_high    = float(prev_row["high"]),
        prev_low     = float(prev_row["low"]),
        timestamp    = int(row["timestamp"]),
    )
