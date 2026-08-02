"""
strategy/strategy_trend_breakout.py — Trend Breakout Strategy (Isolated Mode)
══════════════════════════════════════════════════════════════════════════════

Isolated entry logic for Trend Breakout strategy — completely separate from
RSI Bounce (strategy_logic.py / indicators/engine.py).

ENTRY RULES (validated in 12-month backtest):
  1. ADX >= TB_ADX_TREND_TH  → confirms trend regime
  2. Long:  close > prev_high   (breakout above previous bar high)
  3. Short: close < prev_low    (breakout below previous bar low)
  4. NO EMA filter, NO DI filter, NO RSI filter — pure price breakout in trend

RISK PARAMETERS (use TB_* config vars, NOT the RSI Bounce equivalents):
  - Stop-loss:  TB_SL_ATR_MULT × ATR  (default 0.8)
  - Take-profit: TB_TP_RR_MULT × stop_dist  (default 2.0)

This module is ONLY imported/used when STRATEGY_MODE=trend_breakout.
Default STRATEGY_MODE=rsi_bounce preserves live behavior exactly.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from config import (
    EMA_TREND_LEN, EMA_FAST_LEN, ATR_LEN,
    DI_LEN, ADX_SMOOTH, ADX_EMA, RSI_LEN,
    TB_ADX_TREND_TH, TB_ADX_TREND_TH as ADX_TREND_TH_TB,  # alias for clarity
    TB_SL_ATR_MULT, TB_TP_RR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS,
    COMMISSION_PCT,
    FILTER_ATR_MULT, FILTER_BODY_MULT, FILTER_VOL_ENABLED, FILTER_VOL_MULT,
    FILTER_BODY_TOLERANCE,
)

from risk.calculator import RiskLevels, TrailState


class SignalType(Enum):
    NONE           = "None"
    TREND_LONG     = "Trend Long"
    TREND_SHORT    = "Trend Short"
    # Range signals not used in Trend Breakout mode


@dataclass
class Signal:
    signal_type: SignalType
    is_long:     bool
    is_trend:    bool
    regime:      str   # "TREND" | "NONE"


@dataclass
class IndicatorSnapshot:
    """All indicator values for the latest confirmed bar (Trend Breakout subset)."""
    ema_trend:    float
    ema_fast:     float
    atr:          float
    rsi:          float
    dip:          float    # +DI
    dim:          float    # -DI
    adx:          float    # EMA(5)-smoothed ADX
    adx_raw:      float
    vol_sma:      float
    atr_sma:      float
    trend_regime: bool     # True when ADX >= TB_ADX_TREND_TH
    range_regime: bool     # Not used
    filters_ok:   bool     # ATR + volume + body filters
    atr_ok:       bool
    vol_ok:       bool
    body_ok:      bool
    open:         float
    high:         float
    low:          float
    close:        float
    volume:       float
    prev_high:    float
    prev_low:     float
    timestamp:    int


def _first_valid_idx(arr: np.ndarray) -> int:
    for i, v in enumerate(arr):
        if not np.isnan(v):
            return i
    return -1


def _rma(series: pd.Series, length: int) -> pd.Series:
    arr = series.to_numpy(dtype=np.float64)
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    start = _first_valid_idx(arr)
    if start < 0 or n - start < length:
        return pd.Series(out, index=series.index)
    seed_end = start + length
    seed = float(np.mean(arr[start:seed_end]))
    out[seed_end - 1] = seed
    alpha = 1.0 / length
    for i in range(seed_end, n):
        v = arr[i]
        if np.isnan(v):
            out[i] = out[i - 1]
        else:
            out[i] = out[i - 1] * (1.0 - alpha) + v * alpha
    return pd.Series(out, index=series.index)


def _ema(series: pd.Series, length: int) -> pd.Series:
    arr = series.to_numpy(dtype=np.float64)
    n = len(arr)
    out = np.full(n, np.nan, dtype=np.float64)
    start = _first_valid_idx(arr)
    if start < 0 or n - start < length:
        return pd.Series(out, index=series.index)
    seed_end = start + length
    seed = float(np.mean(arr[start:seed_end]))
    out[seed_end - 1] = seed
    alpha = 2.0 / (length + 1.0)
    for i in range(seed_end, n):
        v = arr[i]
        if np.isnan(v):
            out[i] = out[i - 1]
        else:
            out[i] = out[i - 1] * (1.0 - alpha) + v * alpha
    return pd.Series(out, index=series.index)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    return _rma(_true_range(high, low, close), length)


def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = _rma(gain.fillna(0.0), length)
    avg_loss = _rma(loss.fillna(0.0), length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    return rsi


def _dmi(high: pd.Series, low: pd.Series, close: pd.Series, di_len: int, adx_smooth: int):
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm  = pd.Series(plus_dm,  index=high.index).fillna(0.0)
    minus_dm = pd.Series(minus_dm, index=high.index).fillna(0.0)

    tr = _true_range(high, low, close)
    atr_di = _rma(tr, di_len)
    sm_plus  = _rma(plus_dm,  di_len)
    sm_minus = _rma(minus_dm, di_len)

    plus_di  = 100.0 * sm_plus  / atr_di.replace(0.0, np.nan)
    minus_di = 100.0 * sm_minus / atr_di.replace(0.0, np.nan)
    plus_di  = plus_di.fillna(0.0)
    minus_di = minus_di.fillna(0.0)

    dx_denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / dx_denom
    dx = dx.fillna(0.0)

    adx_raw = _rma(dx, adx_smooth)
    return plus_di, minus_di, adx_raw


def compute_full_series_tb(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators for Trend Breakout backtesting/verification."""
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >= {min_bars} bars, got {len(df)}")

    df = df.reset_index(drop=True).copy()
    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    close  = df["close"].astype(float)
    open_  = df["open"].astype(float)
    volume = df["volume"].astype(float)

    out = pd.DataFrame()
    out["timestamp"] = df["timestamp"].values if "timestamp" in df.columns else np.arange(len(df))
    out["open"]   = open_.values
    out["high"]   = high.values
    out["low"]    = low.values
    out["close"]  = close.values
    out["volume"] = volume.values

    out["ema200"] = _ema(close, EMA_TREND_LEN).values
    out["ema50"]  = _ema(close, EMA_FAST_LEN).values

    atr = _atr(high, low, close, ATR_LEN)
    out["atr"]     = atr.values
    out["atr_sma"] = atr.rolling(50).mean().values

    out["rsi"] = _rsi(close, RSI_LEN).values

    plus_di, minus_di, adx_raw = _dmi(high, low, close, DI_LEN, ADX_SMOOTH)
    out["dip"]     = plus_di.values
    out["dim"]     = minus_di.values
    out["adx_raw"] = adx_raw.values
    out["adx"]     = _ema(adx_raw, ADX_EMA).values

    out["vol_sma"] = volume.rolling(20).mean().values

    return out


def compute_tb(df: pd.DataFrame) -> IndicatorSnapshot:
    """Compute indicators on confirmed OHLCV DataFrame, return latest snapshot."""
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >= {min_bars} bars, got {len(df)}")

    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    ema_trend = float(_ema(close, EMA_TREND_LEN).iloc[-1])
    ema_fast  = float(_ema(close, EMA_FAST_LEN).iloc[-1])

    atr_s   = _atr(high, low, close, ATR_LEN)
    atr     = float(atr_s.iloc[-1])
    atr_sma = float(atr_s.rolling(50).mean().iloc[-1])

    rsi = float(_rsi(close, RSI_LEN).iloc[-1])

    plus_di_s, minus_di_s, adx_raw_s = _dmi(high, low, close, DI_LEN, ADX_SMOOTH)
    dip_val      = float(plus_di_s.iloc[-1])
    dim_val      = float(minus_di_s.iloc[-1])
    adx_raw_val  = float(adx_raw_s.iloc[-1])
    adx_smoothed = float(_ema(adx_raw_s, ADX_EMA).iloc[-1])

    vol_sma = float(df["volume"].rolling(20).mean().iloc[-1])

    # Trend Breakout: ADX >= TB_ADX_TREND_TH confirms trend
    trend_regime = adx_smoothed >= TB_ADX_TREND_TH
    range_regime = False  # Not used in TB mode

    # Filters (same as RSI Bounce for consistency)
    atr_ok  = atr < atr_sma * FILTER_ATR_MULT
    body_ok = abs(float(last["close"]) - float(last["open"])) > atr * (FILTER_BODY_MULT - FILTER_BODY_TOLERANCE)

    if FILTER_VOL_ENABLED:
        bar_vol = float(last["volume"])
        if bar_vol > 0 and vol_sma > 0:
            vol_ok = bar_vol > vol_sma * FILTER_VOL_MULT
        else:
            vol_ok = False
    else:
        vol_ok = True

    filters_ok = atr_ok and vol_ok and body_ok

    return IndicatorSnapshot(
        ema_trend    = ema_trend,
        ema_fast     = ema_fast,
        atr          = atr,
        rsi          = rsi,
        dip          = dip_val,
        dim          = dim_val,
        adx          = adx_smoothed,
        adx_raw      = adx_raw_val,
        vol_sma      = vol_sma,
        atr_sma      = atr_sma,
        trend_regime = bool(trend_regime),
        range_regime = bool(range_regime),
        filters_ok   = bool(filters_ok),
        atr_ok       = bool(atr_ok),
        vol_ok       = bool(vol_ok),
        body_ok      = bool(body_ok),
        open         = float(last["open"]),
        high         = float(last["high"]),
        low          = float(last["low"]),
        close        = float(last["close"]),
        volume       = float(last["volume"]),
        prev_high    = float(prev["high"]),
        prev_low     = float(prev["low"]),
        timestamp    = int(last.get("timestamp", 0)),
    )


def evaluate_trend_breakout(snap: IndicatorSnapshot, has_position: bool = False) -> Signal:
    """
    Evaluate Trend Breakout entry conditions.

    Rules (validated 12-month backtest):
      - ADX >= TB_ADX_TREND_TH confirms trend regime
      - Long:  close > prev_high  (breakout above previous bar high)
      - Short: close < prev_low   (breakout below previous bar low)
      - Filters: ATR + volume + body (same as RSI Bounce)
      - NO EMA trend filter, NO DI filter, NO RSI filter

    Returns Signal(NONE) if in position or no conditions met.
    """
    if has_position:
        return Signal(SignalType.NONE, False, False, "NONE")

    f  = snap.filters_ok
    tr = snap.trend_regime

    # Pure breakout in trend regime — no EMA/DI/RSI filters
    trend_long = (
        tr
        and snap.close > snap.prev_high
        and f
    )
    trend_short = (
        tr
        and snap.close < snap.prev_low
        and f
    )

    if trend_long:
        return Signal(SignalType.TREND_LONG,  is_long=True,  is_trend=True, regime="TREND")
    if trend_short:
        return Signal(SignalType.TREND_SHORT, is_long=False, is_trend=True, regime="TREND")

    return Signal(SignalType.NONE, False, False, "NONE")


def calc_levels_tb(entry_price: float, atr: float, is_long: bool) -> RiskLevels:
    """
    Calculate SL/TP for Trend Breakout using TB_* parameters.

    - Stop distance: min(atr * TB_SL_ATR_MULT, MAX_SL_POINTS)
    - Take-profit:   entry ± stop_dist * TB_TP_RR_MULT

    Note: is_trend is always True for Trend Breakout (only trend signals exist).
    """
    atr_mult  = TB_SL_ATR_MULT
    rr        = TB_TP_RR_MULT
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)

    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + stop_dist * rr
    else:
        sl = entry_price + stop_dist
        tp = entry_price - stop_dist * rr

    return RiskLevels(
        entry_price = entry_price,
        sl          = sl,
        tp          = tp,
        stop_dist   = stop_dist,
        atr         = atr,
        is_long     = is_long,
        is_trend    = True,  # Trend Breakout only produces trend signals
    )