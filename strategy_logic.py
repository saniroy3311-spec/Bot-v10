from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from config import (
    EMA_TREND_LEN, EMA_FAST_LEN, ATR_LEN,
    DI_LEN, ADX_SMOOTH, ADX_EMA, RSI_LEN,
    ADX_TREND_TH, ADX_RANGE_TH,
    FILTER_ATR_MULT, FILTER_BODY_MULT, FILTER_VOL_ENABLED, FILTER_VOL_MULT,
    RSI_OB, RSI_OS,
    TREND_RR, RANGE_RR, TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS, TRAIL_STAGES, BE_MULT,
    COMMISSION_PCT,
)


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
    regime:      str


@dataclass
class IndicatorSnapshot:
    ema_trend:    float
    ema_fast:     float
    atr:          float
    rsi:          float
    dip:          float
    dim:          float
    adx:          float
    adx_raw:      float
    vol_sma:      float
    atr_sma:      float
    trend_regime: bool
    range_regime: bool
    filters_ok:   bool
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


def compute_full_series(df: pd.DataFrame) -> pd.DataFrame:
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >={min_bars} bars, got {len(df)}")

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


def compute(df: pd.DataFrame) -> IndicatorSnapshot:
    series = compute_full_series(df)
    last = series.iloc[-1]
    prev = series.iloc[-2]

    atr_v    = float(last["atr"])
    atr_sma  = float(last["atr_sma"])
    vol_sma  = float(last["vol_sma"])
    bar_vol  = float(last["volume"])
    open_v   = float(last["open"])
    close_v  = float(last["close"])

    atr_ok  = bool(atr_v < atr_sma * FILTER_ATR_MULT)
    body_ok = bool(abs(close_v - open_v) > atr_v * FILTER_BODY_MULT)

    if FILTER_VOL_ENABLED:
        vol_ok = bool(bar_vol > 0 and vol_sma > 0 and bar_vol > vol_sma * FILTER_VOL_MULT)
    else:
        vol_ok = True

    filters_ok = bool(atr_ok and vol_ok and body_ok)

    adx_v = float(last["adx"])
    trend_regime = bool(adx_v > ADX_TREND_TH)
    range_regime = bool(adx_v < ADX_RANGE_TH)

    return IndicatorSnapshot(
        ema_trend    = float(last["ema200"]),
        ema_fast     = float(last["ema50"]),
        atr          = atr_v,
        rsi          = float(last["rsi"]),
        dip          = float(last["dip"]),
        dim          = float(last["dim"]),
        adx          = adx_v,
        adx_raw      = float(last["adx_raw"]),
        vol_sma      = vol_sma,
        atr_sma      = atr_sma,
        trend_regime = trend_regime,
        range_regime = range_regime,
        filters_ok   = filters_ok,
        atr_ok       = atr_ok,
        vol_ok       = vol_ok,
        body_ok      = body_ok,
        open         = open_v,
        high         = float(last["high"]),
        low          = float(last["low"]),
        close        = close_v,
        volume       = bar_vol,
        prev_high    = float(prev["high"]),
        prev_low     = float(prev["low"]),
        timestamp    = int(last["timestamp"]),
    )


def evaluate_entry(snap: IndicatorSnapshot, has_position: bool) -> Signal:
    if has_position:
        return Signal(SignalType.NONE, False, False, "none")

    f  = snap.filters_ok
    tr = snap.trend_regime
    rg = snap.range_regime

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
    range_long  = rg and snap.rsi < RSI_OS and f
    range_short = rg and snap.rsi > RSI_OB and f

    if trend_long:
        return Signal(SignalType.TREND_LONG,  True,  True,  "trend")
    if trend_short:
        return Signal(SignalType.TREND_SHORT, False, True,  "trend")
    if range_long:
        return Signal(SignalType.RANGE_LONG,  True,  False, "range")
    if range_short:
        return Signal(SignalType.RANGE_SHORT, False, False, "range")
    return Signal(SignalType.NONE, False, False, "none")


@dataclass
class RiskLevels:
    entry_price: float
    sl:          float
    tp:          float
    stop_dist:   float
    atr:         float
    is_long:     bool
    is_trend:    bool


@dataclass
class TrailState:
    stage:        int   = 0
    current_sl:   float = 0.0
    peak_price:   float = 0.0
    be_done:      bool  = False
    max_sl_fired: bool  = False


def calc_levels(entry_price: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
    rr       = TREND_RR       if is_trend else RANGE_RR
    atr_mult = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + stop_dist * rr
    else:
        sl = entry_price + stop_dist
        tp = entry_price - stop_dist * rr
    return RiskLevels(entry_price, sl, tp, stop_dist, atr, is_long, is_trend)


def get_trail_params(stage: int, atr: float) -> tuple[float, float]:
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def upgrade_trail_stage(current_stage: int, peak_profit_dist: float, atr: float) -> int:
    new_stage = current_stage
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if peak_profit_dist >= atr * trigger_mult:
            new_stage = max(new_stage, i + 1)
            break
    return new_stage


def compute_trail_sl(stage: int, peak_price: float, peak_profit_dist: float, is_long: bool, atr: float) -> Optional[float]:
    # Pine Script exact parity (matches monitor/trail_loop.py _compute_trail_sl):
    #   trail_points (pts_mult) = ACTIVATION threshold — profit must reach this first.
    #   trail_offset (off_mult) = DISTANCE stop is placed from the peak price.
    #
    # If peak profit has not yet reached the activation threshold for this stage,
    # return None (trail not yet active). Once activated, stop = peak ± offset.
    #
    # Stage activation / offset (ATR multiples from config TRAIL_STAGES):
    #   Stage 1: activate at 0.70 ATR profit, stop 0.55 ATR from peak
    #   Stage 2: activate at 0.55 ATR profit, stop 0.45 ATR from peak
    #   Stage 3: activate at 0.45 ATR profit, stop 0.35 ATR from peak
    #   Stage 4: activate at 0.30 ATR profit, stop 0.25 ATR from peak
    #   Stage 5: activate at 0.20 ATR profit, stop 0.15 ATR from peak
    if stage == 0:
        return None
    _, pts_mult, off_mult = TRAIL_STAGES[max(stage - 1, 0)]
    activation = atr * pts_mult   # profit must be >= this to trail
    offset     = atr * off_mult   # stop placed this far from peak
    if peak_profit_dist < activation:
        return None  # trail not yet activated for this stage
    return (peak_price - offset) if is_long else (peak_price + offset)


def should_trigger_be(profit_dist: float, atr: float) -> bool:
    return profit_dist > atr * BE_MULT


def max_sl_threshold(atr: float) -> float:
    return min(atr * MAX_SL_MULT, MAX_SL_POINTS)


def max_sl_hit(current_price: float, entry_price: float, atr: float, is_long: bool) -> bool:
    threshold = max_sl_threshold(atr)
    if is_long:
        return current_price <= entry_price - threshold
    return current_price >= entry_price + threshold


def calc_real_pl(entry_price: float, exit_price: float, is_long: bool, qty: int) -> float:
    # FIX-COMM: Exit orders on Delta India are bracket/limit → maker fee = 0%.
    # Only the entry leg incurs a taker fee (COMMISSION_PCT).
    # Old code charged BOTH legs: (entry+exit) * qty * (COMMISSION_PCT*2) — WRONG.
    # This matches risk/calculator.py calc_real_pl exactly.
    raw_pl = (exit_price - entry_price) * qty if is_long else (entry_price - exit_price) * qty
    comm   = entry_price * qty * COMMISSION_PCT
    return raw_pl - comm


def signal_log_record(snap: IndicatorSnapshot, sig: Signal, reason: str = "") -> dict:
    return {
        "timestamp":     int(snap.timestamp),
        "candle_open":   round(snap.open, 6),
        "candle_close":  round(snap.close, 6),
        "candle_high":   round(snap.high, 6),
        "candle_low":    round(snap.low, 6),
        "signal_type":   sig.signal_type.value,
        "reason":        reason,
        "indicator_values": {
            "ema_trend":    round(snap.ema_trend, 6),
            "ema_fast":     round(snap.ema_fast, 6),
            "atr":          round(snap.atr, 6),
            "atr_sma":      round(snap.atr_sma, 6),
            "rsi":          round(snap.rsi, 6),
            "adx":          round(snap.adx, 6),
            "adx_raw":      round(snap.adx_raw, 6),
            "dip":          round(snap.dip, 6),
            "dim":          round(snap.dim, 6),
            "vol_sma":      round(snap.vol_sma, 6),
            "trend_regime": snap.trend_regime,
            "range_regime": snap.range_regime,
            "atr_ok":       snap.atr_ok,
            "vol_ok":       snap.vol_ok,
            "body_ok":      snap.body_ok,
            "filters_ok":   snap.filters_ok,
            "prev_high":    round(snap.prev_high, 6),
            "prev_low":     round(snap.prev_low, 6),
        },
    }
