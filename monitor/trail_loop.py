from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from numba import njit

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
    ema_trend: float; ema_fast: float; atr: float; rsi: float
    dip: float; dim: float; adx: float; adx_raw: float
    vol_sma: float; atr_sma: float
    trend_regime: bool; range_regime: bool; filters_ok: bool
    atr_ok: bool; vol_ok: bool; body_ok: bool
    open: float; high: float; low: float; close: float; volume: float
    prev_high: float; prev_low: float; timestamp: int

# ─── PINE SCRIPT EXACT MATH REPLICATION (JIT COMPILED) ────────────────────────

@njit(cache=True)
def _rma_njit(arr: np.ndarray, length: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    start = -1
    for i in range(n):
        if not np.isnan(arr[i]):
            start = i
            break
    if start < 0 or n - start < length:
        return out
    
    seed_sum = 0.0
    for i in range(start, start + length):
        seed_sum += arr[i]
    out[start + length - 1] = seed_sum / length
    
    alpha = 1.0 / length
    for i in range(start + length, n):
        v = arr[i]
        out[i] = out[i - 1] if np.isnan(v) else out[i - 1] * (1.0 - alpha) + v * alpha
    return out

@njit(cache=True)
def _ema_njit(arr: np.ndarray, length: int) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    start = -1
    for i in range(n):
        if not np.isnan(arr[i]):
            start = i
            break
    if start < 0 or n - start < length:
        return out
        
    seed_sum = 0.0
    for i in range(start, start + length):
        seed_sum += arr[i]
    out[start + length - 1] = seed_sum / length
    
    alpha = 2.0 / (length + 1.0)
    for i in range(start + length, n):
        v = arr[i]
        out[i] = out[i - 1] if np.isnan(v) else out[i - 1] * (1.0 - alpha) + v * alpha
    return out

def _ema(series: pd.Series, length: int) -> pd.Series:
    return pd.Series(_ema_njit(series.to_numpy(dtype=np.float64), length), index=series.index)

def _rma(series: pd.Series, length: int) -> pd.Series:
    return pd.Series(_rma_njit(series.to_numpy(dtype=np.float64), length), index=series.index)

def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    return _rma(_true_range(high, low, close), length)

def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = _rma(delta.clip(lower=0.0).fillna(0.0), length)
    loss = _rma((-delta.clip(upper=0.0)).fillna(0.0), length)
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs)).where(loss != 0.0, 100.0)

def _dmi(high: pd.Series, low: pd.Series, close: pd.Series, di_len: int, adx_smooth: int):
    up, down = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    atr_di = _rma(_true_range(high, low, close), di_len).replace(0.0, np.nan)
    plus_di = (100.0 * _rma(plus_dm, di_len) / atr_di).fillna(0.0)
    minus_di = (100.0 * _rma(minus_dm, di_len) / atr_di).fillna(0.0)

    dx = (100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)).fillna(0.0)
    return plus_di, minus_di, _rma(dx, adx_smooth)

# ─── CORE STRATEGY CALCULATION ────────────────────────────────────────────────

def compute_full_series(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    op, hi, lo, cl, vol = df['open'], df['high'], df['low'], df['close'], df['volume']

    df['ema_trend'] = _ema(cl, EMA_TREND_LEN)
    df['ema_fast']  = _ema(cl, EMA_FAST_LEN)
    df['atr']       = _atr(hi, lo, cl, ATR_LEN)
    df['rsi']       = _rsi(cl, RSI_LEN)
    
    dip, dim, adx_raw = _dmi(hi, lo, cl, DI_LEN, ADX_SMOOTH)
    df['dip'], df['dim'], df['adx_raw'] = dip, dim, adx_raw
    df['adx']       = _ema(df['adx_raw'], ADX_EMA)

    df['atr_sma']   = df['atr'].rolling(50).mean()
    df['vol_sma']   = vol.rolling(20).mean()

    df['prev_high'] = hi.shift(1)
    df['prev_low']  = lo.shift(1)

    df['body_abs']  = (cl - op).abs()
    
    return df

def compute(df: pd.DataFrame) -> IndicatorSnapshot:
    df_calc = compute_full_series(df)
    row = df_calc.iloc[-1]

    trend_regime = row['adx'] > ADX_TREND_TH
    range_regime = row['adx'] < ADX_RANGE_TH

    atr_ok  = row['atr'] < (row['atr_sma'] * FILTER_ATR_MULT)
    vol_ok  = (row['volume'] > row['vol_sma'] * FILTER_VOL_MULT) if FILTER_VOL_ENABLED else True
    body_ok = row['body_abs'] > (row['atr'] * FILTER_BODY_MULT)
    filters_ok = atr_ok and vol_ok and body_ok

    return IndicatorSnapshot(
        ema_trend=row['ema_trend'], ema_fast=row['ema_fast'],
        atr=row['atr'], rsi=row['rsi'], dip=row['dip'], dim=row['dim'],
        adx=row['adx'], adx_raw=row['adx_raw'],
        vol_sma=row['vol_sma'], atr_sma=row['atr_sma'],
        trend_regime=trend_regime, range_regime=range_regime,
        filters_ok=filters_ok, atr_ok=atr_ok, vol_ok=vol_ok, body_ok=body_ok,
        open=row['open'], high=row['high'], low=row['low'], close=row['close'],
        volume=row['volume'], prev_high=row['prev_high'], prev_low=row['prev_low'],
        timestamp=int(df.index[-1].timestamp() * 1000)
    )

def evaluate_entry(inds: IndicatorSnapshot) -> Signal:
    if inds.trend_regime and inds.ema_fast > inds.ema_trend and inds.dip > inds.dim and inds.close > inds.prev_high and inds.filters_ok:
        return Signal(SignalType.TREND_LONG, True, True, "TREND")
    if inds.trend_regime and inds.ema_fast < inds.ema_trend and inds.dim > inds.dip and inds.close < inds.prev_low and inds.filters_ok:
        return Signal(SignalType.TREND_SHORT, False, True, "TREND")
    if inds.range_regime and inds.rsi < RSI_OS and inds.filters_ok:
        return Signal(SignalType.RANGE_LONG, True, False, "RANGE")
    if inds.range_regime and inds.rsi > RSI_OB and inds.filters_ok:
        return Signal(SignalType.RANGE_SHORT, False, False, "RANGE")
    return Signal(SignalType.NONE, False, False, "NONE")

def calc_levels(entry_price: float, is_long: bool, is_trend: bool, current_atr: float) -> RiskLevels:
    atr_mult = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr       = TREND_RR       if is_trend else RANGE_RR

    stop_dist = min(current_atr * atr_mult, MAX_SL_POINTS)

    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + (stop_dist * rr)
    else:
        sl = entry_price + stop_dist
        tp = entry_price - (stop_dist * rr)

    return RiskLevels(
        entry_price=entry_price, sl=sl, tp=tp,
        stop_dist=stop_dist, atr=current_atr,
        is_long=is_long, is_trend=is_trend
    )
