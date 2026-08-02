"""
phase1/verify.py
CORE VERIFICATION ENGINE — Phase 1

Compares Python bot indicator values against TradingView exported values.
Produces a pass/fail report with % divergence per indicator per bar.

Usage:
    # Step 1: Run fetch + compute
    python phase1/fetch_ohlcv.py --tf 1h --bars 500
    python phase1/compute_indicators.py

    # Step 2: Export from TradingView
    #   - Add tv_exporter.pine to chart (same TF as above)
    #   - Right-click → Download historical data
    #   - Save as phase1/data/tv_export.csv

    # Step 3: Run comparison
    python phase1/verify.py --python phase1/data/*_indicators.csv --tv phase1/data/tv_export.csv
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
import glob
import pandas as pd
import numpy as np
from tabulate import tabulate
from datetime import datetime

# Tolerance thresholds (% divergence)
PASS_THRESHOLD  = 0.01   # ≤0.01% → PASS (green)
WARN_THRESHOLD  = 0.05   # ≤0.05% → WARN (yellow)
# >0.05% → FAIL (red)

INDICATOR_MAP = {
    # Python col  : TV col (from tv_exporter.pine plot titles)
    "ema200"   : "ema200",
    "ema50"    : "ema50",
    "atr"      : "atr",
    "rsi"      : "rsi",
    "dip"      : "dip",
    "dim"      : "dim",
    "adx_raw"  : "adx_raw",
    "adx"      : "adx",
}


def load_tv_export(path: str) -> pd.DataFrame:
    """
    Load TradingView CSV export.
    TV exports vary slightly in format — this handles both formats.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # TV exports time as Unix timestamp or datetime string
    if "time" in df.columns:
        df["timestamp"] = pd.to_datetime(df["time"]).astype("int64") // 10**6
    elif "timestamp" in df.columns:
        df["timestamp"] = df["timestamp"]

    return df


def align_on_timestamp(py_df: pd.DataFrame, tv_df: pd.DataFrame) -> tuple:
    """Align both DataFrames on matching timestamps."""
    py_df["ts_sec"] = (py_df["timestamp"] / 1000).astype(int)
    tv_df["ts_sec"] = (tv_df["timestamp"] / 1000).astype(int)

    merged = pd.merge(
        py_df, tv_df,
        on="ts_sec",
        suffixes=("_py", "_tv")
    )
    print(f"\nBars after alignment: {len(merged)} "
          f"(Python: {len(py_df)}, TV: {len(tv_df)})")
    return merged


def compute_divergence(merged: pd.DataFrame) -> pd.DataFrame:
    """
    For each indicator, compute % divergence between Python and TV values.
    Returns summary DataFrame.
    """
    rows = []
    for py_col, tv_col in INDICATOR_MAP.items():
        py_key = f"{py_col}_py" if f"{py_col}_py" in merged.columns else py_col
        tv_key = f"{tv_col}_tv" if f"{tv_col}_tv" in merged.columns else tv_col

        if py_key not in merged.columns or tv_key not in merged.columns:
            rows.append({
                "Indicator": py_col, "Status": "⚠️ SKIP",
                "Max Δ%": "N/A", "Avg Δ%": "N/A", "Bad Bars": "N/A"
            })
            continue

        py_vals = merged[py_key].astype(float)
        tv_vals = merged[tv_key].astype(float)

        # % divergence = |py - tv| / |tv| * 100
        denom = tv_vals.abs().replace(0, np.nan)
        pct   = ((py_vals - tv_vals).abs() / denom * 100).fillna(0)

        max_pct  = pct.max()
        avg_pct  = pct.mean()
        bad_bars = (pct > WARN_THRESHOLD).sum()

        if max_pct <= PASS_THRESHOLD:
            status = "✅ PASS"
        elif max_pct <= WARN_THRESHOLD:
            status = "🟡 WARN"
        else:
            status = "❌ FAIL"

        rows.append({
            "Indicator" : py_col,
            "Status"    : status,
            "Max Δ%"    : f"{max_pct:.5f}%",
            "Avg Δ%"    : f"{avg_pct:.5f}%",
            "Bad Bars"  : bad_bars,
        })

    return pd.DataFrame(rows)


def print_sample_comparison(merged: pd.DataFrame, n: int = 5) -> None:
    """Print last N bars side-by-side for visual spot check."""
    print(f"\n{'─'*80}")
    print("SAMPLE — Last 5 bars (Python vs TV)")
    print(f"{'─'*80}")

    for py_col, tv_col in INDICATOR_MAP.items():
        py_key = f"{py_col}_py" if f"{py_col}_py" in merged.columns else py_col
        tv_key = f"{tv_col}_tv" if f"{tv_col}_tv" in merged.columns else tv_col
        if py_key not in merged.columns:
            continue

        tail = merged[[py_key, tv_key]].tail(n)
        tail["diff"] = (tail[py_key] - tail[tv_key]).abs()
        print(f"\n{py_col}:")
        print(tail.to_string(index=False))


def run_self_test() -> None:
    """
    Self-test mode: compute indicators on synthetic data and verify
    internal consistency. Use when TV CSV not available yet.
    """
    print("\n" + "═"*60)
    print("SELF-TEST MODE (no TV CSV provided)")
    print("Verifying indicator engine internal consistency...")
    print("═"*60)

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from phase1.fetch_ohlcv import fetch
    from indicators.engine import compute_full_series, compute

    # Fetch real data from Binance
    try:
        df, _ = fetch("1h", 300)
    except Exception as e:
        print(f"Cannot fetch from Binance: {e}")
        print("Using synthetic data instead...")
        np = __import__("numpy")
        np.random.seed(42)
        n = 300
        close  = 50000 + np.cumsum(np.random.randn(n) * 100)
        df = pd.DataFrame({
            "timestamp": range(n),
            "open" : close - np.random.randn(n)*30,
            "high" : close + np.abs(np.random.randn(n)*50),
            "low"  : close - np.abs(np.random.randn(n)*50),
            "close": close,
            "volume": np.abs(np.random.randn(n)*1000)+500,
        })

    series = compute_full_series(df)
    snap   = compute(df)

    # Verify last bar matches between compute() and compute_full_series()
    checks = {
        "ema200"  : (snap.ema_trend, series["ema200"].iloc[-1]),
        "ema50"   : (snap.ema_fast,  series["ema50"].iloc[-1]),
        "atr"     : (snap.atr,       series["atr"].iloc[-1]),
        "rsi"     : (snap.rsi,       series["rsi"].iloc[-1]),
        "dip"     : (snap.dip,       series["dip"].iloc[-1]),
        "dim"     : (snap.dim,       series["dim"].iloc[-1]),
        "adx"     : (snap.adx,       series["adx"].iloc[-1]),
    }

    rows = []
    all_pass = True
    for name, (snap_val, series_val) in checks.items():
        diff = abs(snap_val - series_val)
        ok   = diff < 1e-8
        if not ok:
            all_pass = False
        rows.append({
            "Indicator" : name,
            "compute()" : f"{snap_val:.6f}",
            "series[-1]": f"{series_val:.6f}",
            "Diff"      : f"{diff:.2e}",
            "Status"    : "✅" if ok else "❌",
        })

    print(tabulate(rows, headers="keys", tablefmt="rounded_outline"))
    print(f"\nLatest bar: close={snap.close:.2f}  ATR={snap.atr:.2f}  ADX={snap.adx:.2f}")
    print(f"Regime: trend={snap.trend_regime}  range={snap.range_regime}")
    print(f"Filters: atr_ok={snap.atr_ok}  vol_ok={snap.vol_ok}  body_ok={snap.body_ok}")

    if all_pass:
        print("\n✅ SELF-TEST PASSED — compute() and compute_full_series() are consistent")
    else:
        print("\n❌ SELF-TEST FAILED — inconsistency between compute modes")

    print("\nNext step:")
    print("  1. Add tv_exporter.pine to your TradingView chart (1h BTCUSDT)")
    print("  2. Right-click chart → Download historical data → save as phase1/data/tv_export.csv")
    print("  3. Run: python phase1/verify.py --tv phase1/data/tv_export.csv")


def run(py_csv: str = None, tv_csv: str = None) -> None:
    if not tv_csv:
        run_self_test()
        return

    print("\n" + "═"*60)
    print("PHASE 1 — INDICATOR VERIFICATION vs TradingView")
    print("═"*60)

    # Load Python indicators
    if not py_csv:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        files    = sorted(glob.glob(os.path.join(data_dir, "*_indicators.csv")))
        if not files:
            print("No indicator CSV found. Run compute_indicators.py first.")
            return
        py_csv = files[-1]

    print(f"\nPython indicators : {py_csv}")
    print(f"TV export         : {tv_csv}")

    py_df = pd.read_csv(py_csv)
    tv_df = load_tv_export(tv_csv)

    merged   = align_on_timestamp(py_df, tv_df)
    summary  = compute_divergence(merged)

    print(f"\n{'═'*60}")
    print("DIVERGENCE REPORT")
    print(f"{'═'*60}")
    print(tabulate(summary, headers="keys", tablefmt="rounded_outline"))

    pass_count = (summary["Status"] == "✅ PASS").sum()
    fail_count = (summary["Status"] == "❌ FAIL").sum()
    warn_count = (summary["Status"] == "🟡 WARN").sum()

    print(f"\nResult: {pass_count} PASS  {warn_count} WARN  {fail_count} FAIL")

    if fail_count == 0:
        print("✅ PHASE 1 COMPLETE — All indicators match TradingView")
        print("   Ready to proceed to Phase 2 (Signal Engine)")
    else:
        print("❌ PHASE 1 FAILED — Fix divergences before proceeding")
        print_sample_comparison(merged)

    # Save detailed report
    out = py_csv.replace("_indicators.csv", "_verification.csv")
    merged.to_csv(out, index=False)
    print(f"\nDetailed comparison saved: {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=None, help="Python indicators CSV")
    parser.add_argument("--tv",     default=None, help="TradingView export CSV")
    args = parser.parse_args()
    run(args.python, args.tv)
