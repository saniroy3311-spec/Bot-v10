from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from config import ALERT_QTY, TRAIL_STAGES

from strategy_logic import (
    compute_full_series,
    evaluate_entry,
    SignalType, Signal, IndicatorSnapshot,
    calc_levels, get_trail_params, upgrade_trail_stage,
    compute_trail_sl, should_trigger_be, max_sl_hit,
    max_sl_threshold, calc_real_pl, signal_log_record,
)


@dataclass
class BTTrade:
    trade_id:     int
    signal_type:  str
    is_long:      bool
    is_trend:     bool
    signal_bar:   int
    signal_ts:    int
    entry_bar:    int
    entry_ts:     int
    entry_price:  float
    sl:           float
    tp:           float
    stop_dist:    float
    atr_at_entry: float
    exit_bar:     int   = 0
    exit_ts:      int   = 0
    exit_price:   float = 0.0
    exit_reason:  str   = ""
    trail_stage:  int   = 0
    bars_held:    int   = 0
    real_pl:      float = 0.0


def _row_to_snap(row, prev_row) -> IndicatorSnapshot:
    from config import ADX_TREND_TH, ADX_RANGE_TH, FILTER_ATR_MULT, FILTER_BODY_MULT, FILTER_VOL_ENABLED
    atr = float(row["atr"])
    atr_sma = float(row["atr_sma"])
    vol_sma = float(row["vol_sma"])
    bar_vol = float(row["volume"])
    open_v  = float(row["open"])
    close_v = float(row["close"])
    atr_ok  = atr < atr_sma * FILTER_ATR_MULT
    body_ok = abs(close_v - open_v) > atr * FILTER_BODY_MULT
    if FILTER_VOL_ENABLED:
        vol_ok = bar_vol > 0 and vol_sma > 0 and bar_vol > vol_sma
    else:
        vol_ok = True
    adx_v = float(row["adx"])
    return IndicatorSnapshot(
        ema_trend    = float(row["ema200"]),
        ema_fast     = float(row["ema50"]),
        atr          = atr,
        rsi          = float(row["rsi"]),
        dip          = float(row["dip"]),
        dim          = float(row["dim"]),
        adx          = adx_v,
        adx_raw      = float(row["adx_raw"]),
        vol_sma      = vol_sma,
        atr_sma      = atr_sma,
        trend_regime = adx_v > ADX_TREND_TH,
        range_regime = adx_v < ADX_RANGE_TH,
        filters_ok   = bool(atr_ok and vol_ok and body_ok),
        atr_ok       = bool(atr_ok),
        vol_ok       = bool(vol_ok),
        body_ok      = bool(body_ok),
        open         = open_v,
        high         = float(row["high"]),
        low          = float(row["low"]),
        close        = close_v,
        volume       = bar_vol,
        prev_high    = float(prev_row["high"]),
        prev_low     = float(prev_row["low"]),
        timestamp    = int(row["timestamp"]),
    )


def _intrabar_exit_long(open_p, high, low, close,
                        sl_price, tp_price, max_sl_active, max_sl_price):
    sl_touched = low <= sl_price
    tp_touched = high >= tp_price
    max_sl_touched = max_sl_active and (low <= max_sl_price)

    if open_p <= sl_price:
        return open_p, "SL"
    if max_sl_touched and open_p <= max_sl_price:
        return open_p, "Max SL"
    if open_p >= tp_price:
        return open_p, "TP"

    if sl_touched and tp_touched:
        return sl_price, "SL"
    if sl_touched:
        return sl_price, "SL"
    if max_sl_touched:
        return max_sl_price, "Max SL"
    if tp_touched:
        return tp_price, "TP"
    return None, None


def _intrabar_exit_short(open_p, high, low, close,
                         sl_price, tp_price, max_sl_active, max_sl_price):
    sl_touched = high >= sl_price
    tp_touched = low <= tp_price
    max_sl_touched = max_sl_active and (high >= max_sl_price)

    if open_p >= sl_price:
        return open_p, "SL"
    if max_sl_touched and open_p >= max_sl_price:
        return open_p, "Max SL"
    if open_p <= tp_price:
        return open_p, "TP"

    if sl_touched and tp_touched:
        return sl_price, "SL"
    if sl_touched:
        return sl_price, "SL"
    if max_sl_touched:
        return max_sl_price, "Max SL"
    if tp_touched:
        return tp_price, "TP"
    return None, None


def run_backtest(df: pd.DataFrame, signal_log_path: Optional[str] = None) -> list[BTTrade]:
    series = compute_full_series(df).reset_index(drop=True)
    n = len(series)
    trades: list[BTTrade] = []

    in_position = False
    pending_signal: Optional[tuple[Signal, IndicatorSnapshot, int]] = None
    trade_id = 0

    cur: Optional[BTTrade] = None
    cur_sl: float = 0.0
    cur_tp: float = 0.0
    cur_atr: float = 0.0
    cur_is_long: bool = True
    cur_entry_price: float = 0.0
    peak_price: float = 0.0
    be_done: bool = False
    trail_stage: int = 0
    max_sl_fired: bool = False
    entry_bar_idx: int = -1

    sig_log_fp = open(signal_log_path, "w", encoding="utf-8") if signal_log_path else None

    def emit(record):
        if sig_log_fp:
            sig_log_fp.write(json.dumps(record, default=str) + "\n")

    for i in range(1, n):
        row = series.iloc[i]
        prev_row = series.iloc[i - 1]
        ts    = int(row["timestamp"])
        open_ = float(row["open"])
        high  = float(row["high"])
        low   = float(row["low"])
        close = float(row["close"])

        if (np.isnan(row["ema200"]) or np.isnan(row["adx"]) or
            np.isnan(row["atr"]) or np.isnan(row["atr_sma"]) or
            np.isnan(row["vol_sma"])):
            continue

        if in_position and pending_signal is not None:
            pending_signal = None

        if pending_signal is not None and not in_position:
            sig, sig_snap, sig_bar_idx = pending_signal
            entry_price = open_
            entry_bar_idx = i
            risk = calc_levels(entry_price, sig_snap.atr, sig.is_long, sig.is_trend)
            trade_id += 1
            cur = BTTrade(
                trade_id     = trade_id,
                signal_type  = sig.signal_type.value,
                is_long      = sig.is_long,
                is_trend     = sig.is_trend,
                signal_bar   = sig_bar_idx,
                signal_ts    = int(series.iloc[sig_bar_idx]["timestamp"]),
                entry_bar    = i,
                entry_ts     = ts,
                entry_price  = entry_price,
                sl           = risk.sl,
                tp           = risk.tp,
                stop_dist    = risk.stop_dist,
                atr_at_entry = sig_snap.atr,
            )
            cur_sl          = risk.sl
            cur_tp          = risk.tp
            cur_atr         = sig_snap.atr
            cur_is_long     = sig.is_long
            cur_entry_price = entry_price
            peak_price      = entry_price
            be_done         = False
            trail_stage     = 0
            max_sl_fired    = False
            in_position     = True
            pending_signal  = None
            emit({
                "event":       "ENTRY",
                "timestamp":   ts,
                "bar_index":   i,
                "signal_bar":  sig_bar_idx,
                "signal_type": sig.signal_type.value,
                "is_long":     sig.is_long,
                "entry_price": entry_price,
                "sl":          risk.sl,
                "tp":          risk.tp,
                "atr":         sig_snap.atr,
            })

        if in_position and cur is not None:
            if cur_is_long:
                peak_price = max(peak_price, high)
                peak_profit_dist = max(0.0, peak_price - cur_entry_price)
                current_profit_dist_close = close - cur_entry_price
            else:
                peak_price = min(peak_price, low)
                peak_profit_dist = max(0.0, cur_entry_price - peak_price)
                current_profit_dist_close = cur_entry_price - close

            if not be_done and should_trigger_be(current_profit_dist_close, cur_atr):
                be_done = True
                if cur_is_long and cur_entry_price > cur_sl:
                    cur_sl = cur_entry_price
                elif (not cur_is_long) and cur_entry_price < cur_sl:
                    cur_sl = cur_entry_price

            new_stage = upgrade_trail_stage(trail_stage, peak_profit_dist, cur_atr)
            if new_stage > trail_stage:
                trail_stage = new_stage

            trail_sl = compute_trail_sl(
                trail_stage, peak_price, peak_profit_dist, cur_is_long, cur_atr
            )
            if trail_sl is not None:
                if cur_is_long and trail_sl > cur_sl:
                    cur_sl = trail_sl
                elif (not cur_is_long) and trail_sl < cur_sl:
                    cur_sl = trail_sl

            max_sl_active = (i > entry_bar_idx) and not max_sl_fired
            threshold = max_sl_threshold(cur_atr)
            if cur_is_long:
                max_sl_price = cur_entry_price - threshold
                exit_price, exit_reason = _intrabar_exit_long(
                    open_, high, low, close,
                    cur_sl, cur_tp, max_sl_active, max_sl_price,
                )
            else:
                max_sl_price = cur_entry_price + threshold
                exit_price, exit_reason = _intrabar_exit_short(
                    open_, high, low, close,
                    cur_sl, cur_tp, max_sl_active, max_sl_price,
                )

            if exit_price is not None:
                if exit_reason == "Max SL":
                    max_sl_fired = True
                cur.exit_bar    = i
                cur.exit_ts     = ts
                cur.exit_price  = exit_price
                cur.exit_reason = exit_reason
                cur.trail_stage = trail_stage
                cur.bars_held   = i - cur.entry_bar
                cur.real_pl     = calc_real_pl(
                    cur.entry_price, exit_price, cur.is_long, ALERT_QTY
                )
                trades.append(cur)
                emit({
                    "event":       "EXIT",
                    "timestamp":   ts,
                    "bar_index":   i,
                    "exit_price":  exit_price,
                    "exit_reason": exit_reason,
                    "trail_stage": trail_stage,
                    "real_pl":     cur.real_pl,
                })
                in_position = False
                cur = None
                continue

        if not in_position and pending_signal is None:
            snap = _row_to_snap(row, prev_row)
            sig = evaluate_entry(snap, has_position=False)
            if sig.signal_type != SignalType.NONE:
                pending_signal = (sig, snap, i)
                emit({
                    "event":       "SIGNAL",
                    "timestamp":   ts,
                    "bar_index":   i,
                    "signal_type": sig.signal_type.value,
                    "candle_open": snap.open,
                    "candle_close": snap.close,
                    "candle_high": snap.high,
                    "candle_low":  snap.low,
                    "atr":         snap.atr,
                    "adx":         snap.adx,
                    "rsi":         snap.rsi,
                    "dip":         snap.dip,
                    "dim":         snap.dim,
                    "ema_fast":    snap.ema_fast,
                    "ema_trend":   snap.ema_trend,
                    "filters_ok":  snap.filters_ok,
                })

    if sig_log_fp:
        sig_log_fp.close()

    return trades


def trades_to_df(trades: list[BTTrade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame([asdict(t) for t in trades])


def qc_report(trades: list[BTTrade], expected_csv: Optional[str] = None) -> dict:
    total = len(trades)
    wins = sum(1 for t in trades if t.real_pl > 0)
    losses = sum(1 for t in trades if t.real_pl <= 0)
    win_rate = (wins / total * 100.0) if total else 0.0
    total_pl = sum(t.real_pl for t in trades)
    best  = max((t.real_pl for t in trades), default=0.0)
    worst = min((t.real_pl for t in trades), default=0.0)

    entry_mismatch = 0
    exit_mismatch  = 0
    timestamp_diffs = []
    expected_count  = None

    if expected_csv and os.path.exists(expected_csv):
        exp = pd.read_csv(expected_csv)
        expected_count = len(exp)

        bot_entries = [(int(t.entry_ts), t.signal_type) for t in trades]
        bot_exits   = [(int(t.exit_ts),  t.exit_reason) for t in trades]

        exp_entry_ts = set()
        exp_exit_ts  = set()
        if "entry_ts" in exp.columns:
            exp_entry_ts = set(int(x) for x in exp["entry_ts"].dropna().tolist())
        if "exit_ts" in exp.columns:
            exp_exit_ts = set(int(x) for x in exp["exit_ts"].dropna().tolist())

        bot_entry_ts_set = set(ts for ts, _ in bot_entries)
        bot_exit_ts_set  = set(ts for ts, _ in bot_exits)

        entry_mismatch = (
            len(bot_entry_ts_set.symmetric_difference(exp_entry_ts))
            if exp_entry_ts else 0
        )
        exit_mismatch = (
            len(bot_exit_ts_set.symmetric_difference(exp_exit_ts))
            if exp_exit_ts else 0
        )

        for ts in sorted(bot_entry_ts_set & exp_entry_ts):
            timestamp_diffs.append(0)

    return {
        "total_trades":      total,
        "wins":              wins,
        "losses":            losses,
        "win_rate_pct":      round(win_rate, 2),
        "total_pl":          round(total_pl, 4),
        "best_trade":        round(best, 4),
        "worst_trade":       round(worst, 4),
        "entry_mismatch":    entry_mismatch,
        "exit_mismatch":     exit_mismatch,
        "expected_count":    expected_count,
        "timestamp_diff_max": max(timestamp_diffs) if timestamp_diffs else 0,
    }


def load_ohlcv_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    rename = {}
    for k in ("timestamp", "time", "date", "open", "high", "low", "close", "volume"):
        if k in cols:
            rename[cols[k]] = k
    df = df.rename(columns=rename)
    if "timestamp" not in df.columns:
        if "time" in df.columns:
            df["timestamp"] = df["time"]
        elif "date" in df.columns:
            df["timestamp"] = pd.to_datetime(df["date"]).astype("int64") // 1_000_000
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").astype("int64")
    if df["timestamp"].iloc[0] < 1_000_000_000_000:
        df["timestamp"] = df["timestamp"] * 1000
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df = df.dropna().sort_values("timestamp").reset_index(drop=True)
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv",      required=True, help="OHLCV CSV path")
    p.add_argument("--out",      default="bt_trades.csv")
    p.add_argument("--signals",  default="bt_signals.jsonl")
    p.add_argument("--expected", default=None, help="Optional Pine trades CSV (entry_ts/exit_ts)")
    args = p.parse_args()

    df = load_ohlcv_csv(args.csv)
    print(f"Loaded {len(df)} bars from {args.csv}")

    trades = run_backtest(df, signal_log_path=args.signals)
    tdf = trades_to_df(trades)
    if not tdf.empty:
        tdf.to_csv(args.out, index=False)
        print(f"Wrote {len(tdf)} trades -> {args.out}")
    else:
        print("No trades produced")

    report = qc_report(trades, expected_csv=args.expected)
    print("\n=== QC REPORT ===")
    for k, v in report.items():
        print(f"  {k:>20s}: {v}")

    rc = 0
    if args.expected:
        if report["entry_mismatch"] != 0 or report["exit_mismatch"] != 0:
            rc = 1
            print("\nQC FAILED: mismatches present")
        else:
            print("\nQC PASSED: zero entry/exit mismatch")
    sys.exit(rc)


if __name__ == "__main__":
    main()
