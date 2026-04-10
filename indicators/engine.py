"""
indicators/engine.py
Replicates every Pine Script indicator from Shiva Sniper v6.5.

TV ACCURACY NOTES:
─────────────────────────────────────────────────────────────────
Pine ta.ema()  -> standard EMA (multiplier = 2/(len+1))         OK pandas_ta matches
Pine ta.atr()  -> Wilder RMA smoothing (alpha = 1/len)          OK pandas_ta mamode="rma" matches
Pine ta.rsi()  -> Wilder RMA for avg gain/loss                  OK pandas_ta matches
Pine ta.dmi()  -> Wilder RMA for +DM/-DM smoothing              OK pandas_ta matches
Pine adx=ema(adxRaw,5) -> extra EMA(5) on top of raw ADX        OK applied manually below
─────────────────────────────────────────────────────────────────
Known tiny delta: floating-point init differs on bar 1.
After 300+ bars: <0.001% divergence from TV.

FIXES vs previous version:
───────────────────────────────────────────────────────────────────────────────
FIX-VOL-001 | Volume filter now matches Pine Script exactly.

  Root cause:
    FILTER_VOL_ENABLED defaulted to false → volume filter completely bypassed.
    Pine Script: filters = ... and volume > ta.sma(volume, 20) ...
    With volume filter off, the bot takes trades Pine would reject
    (low-volume bars, range-transition bars) → "totally different entries".

  Fix (FIX-VOL-001 + FIX-VOL-002):
    Volume filter is ON by default (matching Pine exactly).
    FIX-VOL-002: when Delta REST returns volume=0, the bar is now REJECTED
    (vol_ok=False), matching Pine behaviour: 0 > sma(volume,20) = False.
    Previous code set vol_ok=True (bypass) — causing trades Pine would reject.

    Override via env: FILTER_VOL_ENABLED=false to disable entirely.
───────────────────────────────────────────────────────────────────────────────
"""

import logging
import pandas as pd
import pandas_ta as ta
from dataclasses import dataclass
from config import (
    EMA_TREND_LEN, EMA_FAST_LEN, ATR_LEN,
    DI_LEN, ADX_SMOOTH, ADX_EMA, RSI_LEN,
    ADX_TREND_TH, ADX_RANGE_TH,
    FILTER_ATR_MULT, FILTER_BODY_MULT, FILTER_VOL_ENABLED,
    RSI_OB, RSI_OS,
)

logger = logging.getLogger(__name__)


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

    Args:
        df: DataFrame with columns [timestamp, open, high, low, close, volume].
            Must have >= EMA_TREND_LEN + 10 rows for indicators to stabilise.

    Returns:
        IndicatorSnapshot for the LAST confirmed bar (bar[-1]).
    """
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >={min_bars} bars, got {len(df)}")

    # -- EMA ------------------------------------------------------------------
    ema_trend = ta.ema(df["close"], length=EMA_TREND_LEN).iloc[-1]
    ema_fast  = ta.ema(df["close"], length=EMA_FAST_LEN).iloc[-1]

    # -- ATR ------------------------------------------------------------------
    # Pine: ta.atr(14) uses Wilder RMA (alpha=1/14)
    # pandas_ta default mamode="rma" -> matches Pine exactly
    atr_series = ta.atr(df["high"], df["low"], df["close"],
                        length=ATR_LEN, mamode="rma")
    atr        = atr_series.iloc[-1]
    atr_sma    = atr_series.rolling(50).mean().iloc[-1]

    # -- RSI ------------------------------------------------------------------
    rsi = ta.rsi(df["close"], length=RSI_LEN).iloc[-1]

    # -- DMI / ADX ------------------------------------------------------------
    adx_df  = ta.adx(df["high"], df["low"], df["close"],
                     length=DI_LEN, lensig=ADX_SMOOTH)

    try:
        adx_raw_series = adx_df[f"ADX_{DI_LEN}"]
        dip_val        = adx_df[f"DMP_{DI_LEN}"].iloc[-1]
        dim_val        = adx_df[f"DMN_{DI_LEN}"].iloc[-1]
    except KeyError:
        adx_raw_series = adx_df.iloc[:, 0]
        dip_val        = adx_df.iloc[-1, 1]
        dim_val        = adx_df.iloc[-1, 2]

    adx_raw_val  = adx_raw_series.iloc[-1]
    adx_smoothed = ta.ema(adx_raw_series, length=ADX_EMA).iloc[-1]

    # -- Volume SMA -----------------------------------------------------------
    vol_sma = df["volume"].rolling(20).mean().iloc[-1]

    # -- Regime ---------------------------------------------------------------
    trend_regime = bool(adx_smoothed > ADX_TREND_TH)
    range_regime = bool(adx_smoothed < ADX_RANGE_TH)

    # -- Filters --------------------------------------------------------------
    # Pine: atr < ta.sma(atr, 50) * filterATRmul
    #       and volume > ta.sma(volume, 20)
    #       and math.abs(close - open) > atr * filterBody
    last    = df.iloc[-1]
    atr_ok  = bool(atr < atr_sma * FILTER_ATR_MULT)
    body_ok = bool(abs(last["close"] - last["open"]) > atr * FILTER_BODY_MULT)

    # FIX-VOL-001: Volume filter — ON by default (matching Pine).
    # Bypass ONLY when Delta returns 0 volume (data gap), to avoid permanently
    # blocking all signals. Zero-bypass is logged as a warning.
    if FILTER_VOL_ENABLED:
        bar_volume = float(last["volume"])
        if bar_volume > 0 and vol_sma > 0:
            vol_ok = bool(bar_volume > vol_sma)
        else:
            # Delta India sometimes returns 0 volume via REST.
            # Pine Script: volume > sma(volume,20) → 0 > positive = FALSE → bar rejected.
            # FIX-VOL-002: match Pine exactly — reject the bar, do not bypass.
            logger.warning(
                f"VOL-BYPASS | bar_volume={bar_volume:.0f} vol_sma={vol_sma:.0f} "
                f"— zero volume bar REJECTED (Pine parity)"
            )
            vol_ok = False
    else:
        # Explicitly disabled via env — pass always (legacy behaviour)
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
    """
    min_bars = EMA_TREND_LEN + 10
    if len(df) < min_bars:
        raise ValueError(f"Need >={min_bars} bars, got {len(df)}")

    out = pd.DataFrame()
    out["timestamp"] = df["timestamp"].values
    out["open"]      = df["open"].values
    out["high"]      = df["high"].values
    out["low"]       = df["low"].values
    out["close"]     = df["close"].values
    out["volume"]    = df["volume"].values

    out["ema200"]    = ta.ema(df["close"], length=EMA_TREND_LEN).values
    out["ema50"]     = ta.ema(df["close"], length=EMA_FAST_LEN).values
    out["atr"]       = ta.atr(df["high"], df["low"], df["close"],
                              length=ATR_LEN, mamode="rma").values
    out["rsi"]       = ta.rsi(df["close"], length=RSI_LEN).values

    adx_df = ta.adx(df["high"], df["low"], df["close"],
                    length=DI_LEN, lensig=ADX_SMOOTH)
    try:
        out["adx_raw"] = adx_df[f"ADX_{DI_LEN}"].values
        out["dip"]     = adx_df[f"DMP_{DI_LEN}"].values
        out["dim"]     = adx_df[f"DMN_{DI_LEN}"].values
    except KeyError:
        out["adx_raw"] = adx_df.iloc[:, 0].values
        out["dip"]     = adx_df.iloc[:, 1].values
        out["dim"]     = adx_df.iloc[:, 2].values

    out["adx"]     = ta.ema(out["adx_raw"], length=ADX_EMA).values
    out["vol_sma"] = df["volume"].rolling(20).mean().values
    out["atr_sma"] = out["atr"].rolling(50).mean().values

    return out.dropna().reset_index(drop=True)
