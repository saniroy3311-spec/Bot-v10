"""
phase1/compute_indicators.py
Compute ALL indicators on fetched OHLCV data.
Exports a CSV you import into TradingView for bar-by-bar comparison.

Usage:
    python phase1/compute_indicators.py --csv phase1/data/BTCUSDT_1h_500bars_*.csv
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
import glob
import pandas as pd
from indicators.engine import compute_full_series

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")


def run(csv_path: str) -> str:
    print(f"\nLoading: {csv_path}")
    df = pd.read_csv(csv_path)

    required = ["timestamp", "open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column: {col}")

    print(f"  Bars loaded : {len(df)}")
    print(f"  Computing indicators...")

    result = compute_full_series(df)

    out_path = csv_path.replace(".csv", "_indicators.csv")
    result.to_csv(out_path, index=False)

    print(f"  Output      : {out_path}")
    print(f"  Bars output : {len(result)}")
    print(f"\nLast 3 bars:")
    cols = ["timestamp", "close", "ema200", "ema50", "atr", "rsi", "dip", "dim", "adx_raw", "adx"]
    print(result[cols].tail(3).to_string(index=False))
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=False, help="Path to OHLCV CSV")
    args = parser.parse_args()

    if args.csv:
        run(args.csv)
    else:
        # Auto-find latest CSV in data/
        files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "BTCUSDT_*bars_*.csv")))
        files = [f for f in files if "_indicators" not in f]
        if not files:
            print("No CSV found. Run fetch_ohlcv.py first.")
        else:
            run(files[-1])
