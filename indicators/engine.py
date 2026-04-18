"""
indicators/engine.py
Replicates every Pine Script indicator from Shiva Sniper v6.5.

TV ACCURACY NOTES:
─────────────────────────────────────────────────────────────────
Pine ta.ema()  -> standard EMA (multiplier = 2/(len+1))         OK matches
Pine ta.atr()  -> Wilder RMA smoothing (alpha = 1/len)          OK custom RMA matches
Pine ta.rsi()  -> Wilder RMA for avg gain/loss                  OK custom RMA matches
Pine ta.dmi()  -> Wilder RMA for +DM/-DM smoothing              OK custom RMA matches
Pine adx=ema(adxRaw,5) -> extra EMA(5) on top of raw ADX        OK applied manually

FIX-ENGINE-001 | Replace pandas_ta ADX with custom RMA (Pine-exact)
  ROOT CAUSE:
    pandas_ta seeds its RMA with SMA(first N values) while Pine Script seeds
    it with the first single value (alpha warm-up). This tiny initialisation
    difference propagates forward and can produce ADX values that differ by
    0.1–0.3 near the 18/20 thresholds, flipping regime (TREND/RANGE/NONE)
    on borderline bars — causing the bot to take entries Pine rejects,
    or miss entries Pine takes.
  FIX:
    Use the same custom _rma/_ema/_dmi/_atr/_rsi helpers that strategy_logic.py
    uses for the backtest (already Pine-exact). Removed pandas_ta dependency.
─────────────────────────────────────────────────────────────────
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from config import (
    EMA_TREND_LEN, EMA_FAST_LEN, ATR_LEN,
    DI_LEN, ADX_SMOOTH, ADX_EMA, RSI_LEN,
    ADX_TREND_TH, ADX_RANGE_TH,
    FILTER_ATR_MULT, FILTER_BODY_MULT, FILTER_VOL_ENABLED, FILTER_VOL_MULT,
    RSI_OB, RSI_OS,
)

logger = logging.getLogger(__name__)


# ─── Custom Pine-exact indicator helpers (mirrors strategy_logic.py) ──────────

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
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def _atr_series(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    return _rma(_true_range(high, low, close), length)


def _rsi_series(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))
    avg_gain = _rma(gain.fillna(0.0), length)
    avg_loss = _rma(loss.fillna(0.0), length)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    return rsi


def _dmi_series(high: pd.Series, low: pd.Series, close: pd.Series, di_len: int, adx_smooth: int):
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm   = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index).fillna(0.0)
    minus_dm  = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index).fillna(0.0)
    tr        = _true_range(high, low, close)
    atr_di    = _rma(tr, di_len)
    plus_di   = (100.0 * _rma(plus_dm,  di_len) / atr_di.replace(0.0, np.nan)).fillna(0.0)
    minus_di  = (100.0 * _rma(minus_dm, di_len) / atr_di.replace(0.0, np.nan)).fillna(0.0)
    dx_denom  = (plus_di + minus_di).replace(0.0, np.nan)
    dx        = (100.0 * (plus_di - minus_di).abs() / dx_denom).fillna(0.0)
    adx_raw   = _rma(dx, adx_smooth)
    return plus_di, minus_di, adx_raw


@dataclass
class IndicatorSnapshot:
    """All indicator values for the latest confirmed bar."""
    ema_trend:    float
    ema_fast:     float
    atr:          float
    rsi:          float
    dip:          float   # +DI
    dim:          float   # -DI
    adx:          float   # EMA(5)-smoothed ADX — mirrors Pine exactly
    adx_raw:      float   # Raw ADX before EMA(5) smoothing
    vol_sma:      float   # SMA(volume, 20)
    atr_sma:      float   # SMA(atr, 50)
    # Derived regime + filters
    trend_regime: bool
    range_regime: bool
    filters_ok:   bool
    atr_ok:       bool
    vol_ok:       bool
    body_ok:      bool
    # Raw OHLCV of last bar
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    float
    prev_high: float
    prev_low:  float
    timestamp: int


def compute(df: pd.DataFrame) -> IndicatorSnapshot:
    """
    Compute all indicators on a confirmed OHLCV DataFrame.
    Uses custom Pine-exact RMA helpers (FIX-ENGINE-001).
    """
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >={min_bars} bars, got {len(df)}")

    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    close  = df["close"].astype(float)
    last   = df.iloc[-1]

    ema_trend = _ema(close, EMA_TREND_LEN).iloc[-1]
    ema_fast  = _ema(close, EMA_FAST_LEN).iloc[-1]

    atr_s   = _atr_series(high, low, close, ATR_LEN)
    atr     = atr_s.iloc[-1]
    atr_sma = atr_s.rolling(50).mean().iloc[-1]

    rsi = _rsi_series(close, RSI_LEN).iloc[-1]

    plus_di_s, minus_di_s, adx_raw_s = _dmi_series(high, low, close, DI_LEN, ADX_SMOOTH)
    dip_val     = plus_di_s.iloc[-1]
    dim_val     = minus_di_s.iloc[-1]
    adx_raw_val = adx_raw_s.iloc[-1]
    adx_smoothed = _ema(adx_raw_s, ADX_EMA).iloc[-1]

    vol_sma = df["volume"].rolling(20).mean().iloc[-1]

    trend_regime = bool(adx_smoothed > ADX_TREND_TH)
    range_regime = bool(adx_smoothed < ADX_RANGE_TH)

    atr_ok  = bool(atr < atr_sma * FILTER_ATR_MULT)
    body_ok = bool(abs(float(last["close"]) - float(last["open"])) > atr * FILTER_BODY_MULT)

    if FILTER_VOL_ENABLED:
        bar_volume = float(last["volume"])
        if bar_volume > 0 and vol_sma > 0:
            vol_ok = bool(bar_volume > vol_sma * FILTER_VOL_MULT)
        else:
            logger.warning(
                f"VOL-BYPASS | bar_volume={bar_volume:.0f} vol_sma={vol_sma:.0f} "
                f"— zero volume bar REJECTED (Pine parity)"
            )
            vol_ok = False
    else:
        vol_ok = True

    filters = atr_ok and vol_ok and body_ok

    return IndicatorSnapshot(
        ema_trend    = float(ema_trend),
        ema_fast     = float(ema_fast),
        atr          = float(atr),
        rsi          = float(rsi),
        dip          = float(dip_val),
        dim          = float(dim_val),
        adx          = float(adx_smoothed),
        adx_raw      = float(adx_raw_val),
        vol_sma      = float(vol_sma),
        atr_sma      = float(atr_sma),
        trend_regime = trend_regime,
        range_regime = range_regime,
        filters_ok   = filters,
        atr_ok       = atr_ok,
        vol_ok       = vol_ok,
        body_ok      = body_ok,
        open         = float(last["open"]),
        high         = float(last["high"]),
        low          = float(last["low"]),
        close        = float(last["close"]),
        volume       = float(last["volume"]),
        prev_high    = float(df.iloc[-2]["high"]),
        prev_low     = float(df.iloc[-2]["low"]),
        timestamp    = int(last.get("timestamp", 0)),
    )


def compute_full_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ALL indicator values for the entire DataFrame.
    Used by phase1_verify.py to produce a comparison CSV.
    Uses custom Pine-exact RMA helpers (FIX-ENGINE-001).
    """
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >={min_bars} bars, got {len(df)}")

    high   = df["high"].astype(float)
    low    = df["low"].astype(float)
    close  = df["close"].astype(float)

    out = pd.DataFrame()
    out["timestamp"] = df["timestamp"].values
    out["open"]      = df["open"].values
    out["high"]      = high.values
    out["low"]       = low.values
    out["close"]     = close.values
    out["volume"]    = df["volume"].values

    out["ema200"] = _ema(close, EMA_TREND_LEN).values
    out["ema50"]  = _ema(close, EMA_FAST_LEN).values

    atr_s = _atr_series(high, low, close, ATR_LEN)
    out["atr"]     = atr_s.values
    out["atr_sma"] = atr_s.rolling(50).mean().values

    out["rsi"] = _rsi_series(close, RSI_LEN).values

    plus_di_s, minus_di_s, adx_raw_s = _dmi_series(high, low, close, DI_LEN, ADX_SMOOTH)
    out["dip"]     = plus_di_s.values
    out["dim"]     = minus_di_s.values
    out["adx_raw"] = adx_raw_s.values
    out["adx"]     = _ema(adx_raw_s, ADX_EMA).values
    out["vol_sma"] = df["volume"].rolling(20).mean().values

    return out.dropna().reset_index(drop=True)
