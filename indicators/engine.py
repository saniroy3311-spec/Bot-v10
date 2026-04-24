"""
indicators/engine.py — Shiva Sniper v6.5 (numba-accelerated, Pine-exact)

FIXES IN THIS VERSION
─────────────────────────────────────────────────────────────────────────────
FIX-1  | Added RiskLevels + TrailState import from risk.calculator.
         calc_levels() returned RiskLevels but it was never defined/imported,
         causing a NameError at runtime.

FIX-2  | calc_levels() argument order corrected to match risk/calculator.py
         and all callers in main.py:
           WRONG:   calc_levels(entry_price, is_long, is_trend, current_atr)
           CORRECT: calc_levels(entry_price, atr, is_long, is_trend)
         Passing atr as 'is_long' (a float into a bool slot) silently produced
         wrong stop distances and inverted long/short direction on some trades.

FIX-3  | evaluate_entry() renamed to evaluate() and given has_position param.
         main.py calls: evaluate(snap, has_position=False)
         strategy/signal.py declares: evaluate(snap, has_position) -> Signal
         The old name caused an ImportError or called the wrong function.

FIX-4  | numba import made optional with a pure-numpy fallback.
         If numba is not installed on the VPS the bot crashed on startup
         with ImportError. The fallback uses identical logic without @njit.
         Install numba for a ~4-8× speedup on large DataFrames:
           pip install numba --break-system-packages

FIX-5  | _dmi() renamed _dmi_series() to avoid shadowing the module-level
         helper. The old name conflict caused the wrong function to be called
         inside compute_full_series().

PRESERVED FROM ORIGINAL
─────────────────────────────────────────────────────────────────────────────
  - All Pine-exact indicator maths (RMA, EMA, ATR, RSI, DMI)
  - numba JIT with cache=True for fast per-bar recomputation
  - compute() and compute_full_series() signatures unchanged
  - IndicatorSnapshot, Signal, SignalType dataclasses
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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

# FIX-1: RiskLevels and TrailState were used but never imported or defined.
from risk.calculator import RiskLevels, TrailState

logger = logging.getLogger(__name__)


# ─── FIX-4: numba with graceful fallback ──────────────────────────────────────
# If numba is installed:   JIT-compiled loops → ~4-8× faster on large DFs.
# If not installed:        identical pure-numpy/Python logic, no crash.
# To install:  pip install numba --break-system-packages

try:
    from numba import njit
    _NUMBA_AVAILABLE = True
    logger.debug("numba available — JIT compilation enabled")
except ImportError:
    _NUMBA_AVAILABLE = False
    logger.warning(
        "numba not installed — falling back to pure numpy. "
        "Install with: pip install numba --break-system-packages"
    )

    # Transparent no-op decorator so the functions below work unchanged.
    def njit(*args, **kwargs):
        def decorator(fn):
            return fn
        # Called as @njit or @njit(cache=True) — handle both forms.
        if len(args) == 1 and callable(args[0]):
            return args[0]
        return decorator


# ─── Dataclasses ──────────────────────────────────────────────────────────────

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
    regime:      str   # "TREND" | "RANGE" | "NONE"


@dataclass
class IndicatorSnapshot:
    """All indicator values for the latest confirmed bar."""
    ema_trend:    float
    ema_fast:     float
    atr:          float
    rsi:          float
    dip:          float    # +DI
    dim:          float    # -DI
    adx:          float    # EMA(5)-smoothed ADX — mirrors Pine exactly
    adx_raw:      float    # Raw ADX before EMA(5) smoothing
    vol_sma:      float    # SMA(volume, 20)
    atr_sma:      float    # SMA(atr, 50)
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


# ─── JIT-compiled inner loops ─────────────────────────────────────────────────
# These are the only functions that benefit from numba — tight numeric loops
# over large arrays. All other logic stays in plain Python.

@njit(cache=True)
def _rma_njit(arr: np.ndarray, length: int) -> np.ndarray:
    """
    Wilder's Moving Average — exact match to Pine's ta.rma().
    Seeds with SMA of the first `length` valid values, then applies
    alpha = 1/length recursively.
    """
    n = len(arr)
    out = np.full(n, np.nan)

    # Find first non-NaN index
    start = -1
    for i in range(n):
        if not np.isnan(arr[i]):
            start = i
            break
    if start < 0 or n - start < length:
        return out

    # Seed: SMA of first `length` values
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
    """
    Exponential Moving Average — exact match to Pine's ta.ema().
    Seeds with SMA of the first `length` valid values, then applies
    alpha = 2/(length+1) recursively.
    """
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


# ─── Pandas wrappers around JIT kernels ───────────────────────────────────────

def _rma(series: pd.Series, length: int) -> pd.Series:
    return pd.Series(
        _rma_njit(series.to_numpy(dtype=np.float64), length),
        index=series.index,
    )


def _ema(series: pd.Series, length: int) -> pd.Series:
    return pd.Series(
        _ema_njit(series.to_numpy(dtype=np.float64), length),
        index=series.index,
    )


# ─── Indicator helpers (plain numpy — no inner loops, JIT not needed) ─────────

def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Exact match to Pine's ta.tr() with na handling on bar 0."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low,
         (high - prev_close).abs(),
         (low  - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]   # Pine: na prev_close → H-L
    return tr


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """ATR — Wilder RMA of True Range. Matches Pine's ta.atr()."""
    return _rma(_true_range(high, low, close), length)


def _rsi_series(close: pd.Series, length: int) -> pd.Series:
    """RSI — Wilder RMA of gains/losses. Matches Pine's ta.rsi()."""
    delta    = close.diff()
    gain     = _rma(delta.clip(lower=0.0).fillna(0.0), length)
    loss     = _rma((-delta.clip(upper=0.0)).fillna(0.0), length)
    rs       = gain / loss.replace(0.0, np.nan)
    rsi      = 100.0 - (100.0 / (1.0 + rs))
    rsi      = rsi.where(loss != 0.0, 100.0)   # loss == 0 → RSI = 100
    return rsi


def _dmi_series(
    high: pd.Series, low: pd.Series, close: pd.Series,
    di_len: int, adx_smooth: int,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Directional Movement Index — matches Pine's ta.dmi() exactly.
    Returns (+DI, -DI, raw ADX).
    Note: Pine adds a further EMA(5) on top of raw ADX; apply that separately.

    FIX-5: renamed from _dmi() to _dmi_series() to avoid name collision.
    """
    up_move   = high.diff()
    down_move = -low.diff()

    plus_dm  = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    ).fillna(0.0)
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    ).fillna(0.0)

    tr       = _true_range(high, low, close)
    atr_di   = _rma(tr, di_len).replace(0.0, np.nan)

    plus_di  = (100.0 * _rma(plus_dm,  di_len) / atr_di).fillna(0.0)
    minus_di = (100.0 * _rma(minus_dm, di_len) / atr_di).fillna(0.0)

    dx_denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx       = (100.0 * (plus_di - minus_di).abs() / dx_denom).fillna(0.0)
    adx_raw  = _rma(dx, adx_smooth)

    return plus_di, minus_di, adx_raw


# ─── Main compute functions ────────────────────────────────────────────────────

def compute(df: pd.DataFrame) -> IndicatorSnapshot:
    """
    Compute all indicators on a confirmed OHLCV DataFrame and return
    a snapshot of the latest bar.

    Requires at least EMA_TREND_LEN + 10 bars (default 210).
    Called once per bar close from main.py.
    """
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

    atr_s   = _atr_series(high, low, close, ATR_LEN)
    atr     = float(atr_s.iloc[-1])
    atr_sma = float(atr_s.rolling(50).mean().iloc[-1])

    rsi = float(_rsi_series(close, RSI_LEN).iloc[-1])

    plus_di_s, minus_di_s, adx_raw_s = _dmi_series(high, low, close, DI_LEN, ADX_SMOOTH)
    dip_val      = float(plus_di_s.iloc[-1])
    dim_val      = float(minus_di_s.iloc[-1])
    adx_raw_val  = float(adx_raw_s.iloc[-1])
    adx_smoothed = float(_ema(adx_raw_s, ADX_EMA).iloc[-1])

    vol_sma = float(df["volume"].rolling(20).mean().iloc[-1])

    trend_regime = adx_smoothed > ADX_TREND_TH
    range_regime = adx_smoothed < ADX_RANGE_TH

    atr_ok  = atr < atr_sma * FILTER_ATR_MULT
    body_ok = abs(float(last["close"]) - float(last["open"])) > atr * FILTER_BODY_MULT

    if FILTER_VOL_ENABLED:
        bar_vol = float(last["volume"])
        if bar_vol > 0 and vol_sma > 0:
            vol_ok = bar_vol > vol_sma * FILTER_VOL_MULT
        else:
            logger.warning(
                f"VOL-BYPASS | bar_volume={bar_vol:.0f} vol_sma={vol_sma:.0f} "
                "— zero volume bar rejected (Pine parity)"
            )
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


def compute_full_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ALL indicator values across the entire DataFrame.
    Used by backtest / phase verification scripts.
    Returns a clean DataFrame with NaN rows dropped.
    """
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >= {min_bars} bars, got {len(df)}")

    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)

    out = pd.DataFrame()
    out["timestamp"] = df["timestamp"].values
    out["open"]      = df["open"].values
    out["high"]      = high.values
    out["low"]       = low.values
    out["close"]     = close.values
    out["volume"]    = df["volume"].values

    out["ema200"] = _ema(close, EMA_TREND_LEN).values
    out["ema50"]  = _ema(close, EMA_FAST_LEN).values

    atr_s         = _atr_series(high, low, close, ATR_LEN)
    out["atr"]    = atr_s.values
    out["atr_sma"]= atr_s.rolling(50).mean().values

    out["rsi"] = _rsi_series(close, RSI_LEN).values

    plus_di_s, minus_di_s, adx_raw_s = _dmi_series(high, low, close, DI_LEN, ADX_SMOOTH)
    out["dip"]     = plus_di_s.values
    out["dim"]     = minus_di_s.values
    out["adx_raw"] = adx_raw_s.values
    out["adx"]     = _ema(adx_raw_s, ADX_EMA).values

    out["vol_sma"] = df["volume"].rolling(20).mean().values

    return out.dropna().reset_index(drop=True)


# ─── Entry signal evaluation ───────────────────────────────────────────────────

def evaluate(snap: IndicatorSnapshot, has_position: bool = False) -> Signal:
    """
    Evaluate Pine Script entry conditions for the confirmed bar.

    FIX-3: renamed from evaluate_entry() to evaluate() and added
    has_position parameter to match main.py's call:
        sig = evaluate(snap, has_position=False)

    Maps 1:1 to Pine Script:
        trendLong  = trendRegime and emaFast > emaTrend and dip > dim
                     and close > high[1] and filters
        trendShort = trendRegime and emaFast < emaTrend and dim > dip
                     and close < low[1] and filters
        rangeLong  = rangeRegime and rsi < rsiOS and filters
        rangeShort = rangeRegime and rsi > rsiOB and filters

    Returns Signal(NONE) if in position or no conditions met.
    NOTE: Pine has NO bar-close signal-reversal exit. Exits are
    handled entirely by trail_loop.py at tick resolution.
    """
    if has_position:
        return Signal(SignalType.NONE, False, False, "NONE")

    f  = snap.filters_ok
    tr = snap.trend_regime
    rr = snap.range_regime

    # Pine: close > high[1]  →  snap.close > snap.prev_high
    # Pine: close < low[1]   →  snap.close < snap.prev_low
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
    range_long  = rr and snap.rsi < RSI_OS and f
    range_short = rr and snap.rsi > RSI_OB and f

    # Priority matches Pine: trendLong → trendShort → rangeLong → rangeShort
    if trend_long:
        return Signal(SignalType.TREND_LONG,  is_long=True,  is_trend=True,  regime="TREND")
    if trend_short:
        return Signal(SignalType.TREND_SHORT, is_long=False, is_trend=True,  regime="TREND")
    if range_long:
        return Signal(SignalType.RANGE_LONG,  is_long=True,  is_trend=False, regime="RANGE")
    if range_short:
        return Signal(SignalType.RANGE_SHORT, is_long=False, is_trend=False, regime="RANGE")

    return Signal(SignalType.NONE, False, False, "NONE")


# ─── Risk level calculation ────────────────────────────────────────────────────

def calc_levels(entry_price: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
    """
    Compute SL and TP from entry price + ATR.

    FIX-2: argument order corrected from (entry, is_long, is_trend, atr)
            to (entry, atr, is_long, is_trend) — matches risk/calculator.py
            and every call in main.py:
                calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

    FIX-1: RiskLevels is now imported from risk.calculator so this
            function can actually instantiate and return it.
    """
    atr_mult  = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr        = TREND_RR       if is_trend else RANGE_RR
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
        is_trend    = is_trend,
    )
