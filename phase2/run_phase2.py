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

═══════════════════════════════════════════════════════════════════
BUG FIXES IN THIS VERSION:
═══════════════════════════════════════════════════════════════════

BUG-PE-001 | Trail SL used `pts` (activation distance) instead of
             `off` (trailing distance behind peak).

  Old code:
    pts, _ = get_trail_params(state.trail_stage, t.atr_at_entry)
    candidate = peak_price - pts   <- WRONG: pts is activation, not offset

  Pine strategy.exit():
    trail_points = activePts  (profit distance to ACTIVATE trail)
    trail_offset = activeOff  (distance from PEAK where SL sits)
    Trail SL = peak_price - activeOff  [long]

  FIX:
    _, off = get_trail_params(state.trail_stage, t.atr_at_entry)
    candidate = peak_price - off   <- CORRECT: off is the trail distance

BUG-PE-002 | Max SL exit price hardcoded 1.5x ATR / 500pt cap.

  Old code:
    exit_price = t.entry_price - min(t.atr_at_entry * 1.5, 500.0)

  Pine (30M-OPT-002, 30M-OPT-008):
    maxSLmult = 2.0  (MAX_SL_MULT in config)
    maxSLPoints = 1500  (MAX_SL_POINTS in config)
    threshold = min(atr * maxSLmult, maxSLPoints)

  FIX: use max_sl_exit_price() from risk.calculator which reads config.
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
    should_trigger_be, max_sl_hit, max_sl_exit_price,
    calc_real_pl, RiskLevels,
)
from config import ALERT_QTY, COMMISSION_PCT


@dataclass
class PaperTrade:
    """One complete trade record."""
    trade_id:     int
    signal_type:  str
    is_long:      bool
    entry_bar:    int
    entry_ts:     int
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
    peak_price:   float          = 0.0
    be_done:      bool           = False
    trail_stage:  int            = 0
    max_sl_fired: bool           = False


def run(df: pd.DataFrame) -> list:
    """
    Run paper trading on full OHLCV DataFrame.
    Returns list of completed PaperTrade objects.
    """
    series   = compute_full_series(df)
    trades   = []
    state    = EngineState()
    trade_id = 0

    for i in range(1, len(series)):
        row      = series.iloc[i]
        prev_row = series.iloc[i - 1]

        ts    = int(row["timestamp"])
        high  = float(row["high"])
        low   = float(row["low"])
        close = float(row["close"])

        snap = _row_to_snap(row, prev_row)

        # ── OPEN POSITION: evaluate exit conditions ───────────────────
        if state.in_position:
            t = state.trade

            # Track peak price (Pine: highest high for long, lowest low for short)
            if t.is_long:
                state.peak_price = max(state.peak_price, high)
            else:
                state.peak_price = min(state.peak_price, low)

            # Profit distance based on CLOSE — matches Pine's profitDist exactly:
            #   profitDist = (strategy.position_size > 0 ? close - entryPrice
            #                                             : entryPrice - close)
            profit_dist = (close - t.entry_price) if t.is_long \
                          else (t.entry_price - close)

            # Trail stage ratchet using close-based profit_dist (Pine parity)
            new_stage = calc_trail_stage(profit_dist, t.atr_at_entry)
            if new_stage > state.trail_stage:
                state.trail_stage = new_stage
                t.trail_stage     = new_stage

            # Breakeven (Pine: if close - entryPrice > beTrigger)
            if not state.be_done and should_trigger_be(profit_dist, t.atr_at_entry):
                state.current_sl = t.entry_price
                state.be_done    = True

            # ── Trail ratchet SL — BUG-PE-001 FIX ────────────────────
            # Pine: trail_sl = peak_price - activeOFF  [long]
            #   activeOFF = atr * trailNOff  (the OFFSET, NOT activation pts)
            # OLD BUG: used `pts` (activation) as the SL distance.
            # FIX: use `off` (index [1] from get_trail_params).
            active_pts, active_off = get_trail_params(state.trail_stage, t.atr_at_entry)

            # Trail only activates once profit >= activePts (Pine: trail_points)
            peak_profit = (
                (state.peak_price - t.entry_price) if t.is_long
                else (t.entry_price - state.peak_price)
            )
            if peak_profit >= active_pts:
                # SL = peak +/- active_off  (BUG-PE-001 FIX: off not pts)
                if t.is_long:
                    candidate = state.peak_price - active_off
                    if candidate > state.current_sl:
                        state.current_sl = candidate
                else:
                    candidate = state.peak_price + active_off
                    if candidate < state.current_sl:
                        state.current_sl = candidate

            # ── Check exits (intra-bar using high/low) ────────────────
            exit_price  = None
            exit_reason = None

            if t.is_long:
                if high >= t.tp:
                    exit_price  = t.tp
                    exit_reason = "TP"
                elif low <= state.current_sl:
                    exit_price  = state.current_sl
                    if state.trail_stage > 0 and state.current_sl > t.sl:
                        exit_reason = f"Trail S{state.trail_stage}"
                    elif state.be_done and abs(state.current_sl - t.entry_price) < 0.01:
                        exit_reason = "Breakeven SL"
                    else:
                        exit_reason = "Initial SL"
                # BUG-PE-002 FIX: use config values (2.0x / 1500pts)
                elif max_sl_hit(low, t.entry_price, t.atr_at_entry, True):
                    exit_price  = max_sl_exit_price(t.entry_price, t.atr_at_entry, True)
                    exit_reason = "Max SL"
            else:
                if low <= t.tp:
                    exit_price  = t.tp
                    exit_reason = "TP"
                elif high >= state.current_sl:
                    exit_price  = state.current_sl
                    if state.trail_stage > 0 and state.current_sl < t.sl:
                        exit_reason = f"Trail S{state.trail_stage}"
                    elif state.be_done and abs(state.current_sl - t.entry_price) < 0.01:
                        exit_reason = "Breakeven SL"
                    else:
                        exit_reason = "Initial SL"
                # BUG-PE-002 FIX: use config values (2.0x / 1500pts)
                elif max_sl_hit(high, t.entry_price, t.atr_at_entry, False):
                    exit_price  = max_sl_exit_price(t.entry_price, t.atr_at_entry, False)
                    exit_reason = "Max SL"

            if exit_price is not None:
                t.exit_bar    = i
                t.exit_ts     = ts
                t.exit_price  = exit_price
                t.exit_reason = exit_reason
                t.bars_held   = i - t.entry_bar
                t.real_pl     = calc_real_pl(
                    t.entry_price, exit_price, t.is_long, ALERT_QTY
                )
                trades.append(t)
                state = EngineState()
            continue

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
        state.peak_price  = close
        state.be_done     = False
        state.trail_stage = 0

    return trades


def trades_to_df(trades: list) -> pd.DataFrame:
    """Convert trade list to DataFrame."""
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


# ── Internal helper ────────────────────────────────────────────────

def _row_to_snap(row, prev_row):
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
