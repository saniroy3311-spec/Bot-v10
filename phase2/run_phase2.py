"""
phase2/run_phase2.py
ONE COMMAND - runs entire Phase 2.

Usage:
    python phase2/run_phase2.py
    python phase2/run_phase2.py --tv phase2/data/tv_signals.csv
    python phase2/run_phase2.py --tv phase2/data/tv_signals.csv \
        --tv-pl 18347 --tv-trades 1362 --tv-winrate 59.1 --tv-pf 2.94
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse, glob
import pandas as pd
from phase1.fetch_ohlcv    import fetch
from phase2.paper_engine   import run as paper_run, trades_to_df
from phase2.paper_report   import generate as gen_report, print_report
from phase2.verify_signals import (
    load_tv_signals, load_python_signals,
    compare, print_report as print_sig_report,
)

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║       SHIVA SNIPER BOT - PHASE 2: SIGNAL ENGINE             ║
║           Paper trade + compare entry bars vs TV             ║
╚══════════════════════════════════════════════════════════════╝
"""
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "phase1", "data")
OUT_DIR  = os.path.join(os.path.dirname(__file__), "data")


def find_latest_ohlcv():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "BTCUSDT_*bars_*.csv")))
    files = [f for f in files if "_indicators" not in f]
    return files[-1] if files else None


def main():
    print(BANNER)
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv",        default=None)
    parser.add_argument("--tv",         default=None, help="TV signal export CSV")
    parser.add_argument("--tf",         default="1h")
    parser.add_argument("--bars",       default=500, type=int)
    parser.add_argument("--tv-pl",      default=None, type=float)
    parser.add_argument("--tv-trades",  default=None, type=int)
    parser.add_argument("--tv-winrate", default=None, type=float)
    parser.add_argument("--tv-pf",      default=None, type=float)
    args = parser.parse_args()

    # Step 1: Load OHLCV
    print("STEP 1 - Loading OHLCV data")
    print("-" * 60)
    csv_path = args.csv or find_latest_ohlcv()
    if not csv_path:
        print("  No CSV found - fetching fresh data...")
        df, csv_path = fetch(args.tf, args.bars)
    else:
        print(f"  Using: {csv_path}")
        df = pd.read_csv(csv_path)
    print(f"  Bars: {len(df)}")

    # Step 2: Run paper trading
    print("\nSTEP 2 - Running paper trading engine")
    print("-" * 60)
    trades = paper_run(df)
    trades_df = trades_to_df(trades)
    print(f"  Trades completed: {len(trades_df)}")

    if trades_df.empty:
        print("  No trades generated - check indicator warmup bars")
        return

    # Save trades
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "paper_trades.csv")
    trades_df.to_csv(out_path, index=False)
    print(f"  Saved: {out_path}")

    # Step 3: Performance report
    print("\nSTEP 3 - Performance report")
    print("-" * 60)
    metrics = gen_report(trades_df)
    tv_metrics = None
    if args.tv_pl is not None:
        tv_metrics = {
            "total_pl"      : args.tv_pl,
            "total_trades"  : args.tv_trades or 0,
            "win_rate"      : args.tv_winrate or 0,
            "profit_factor" : args.tv_pf or 0,
            "pct_return"    : round(args.tv_pl / 10000 * 100, 2),
        }
    print_report(metrics, tv_metrics)

    # Step 4: Signal comparison vs TV
    if args.tv:
        print("\nSTEP 4 - Signal comparison vs TradingView")
        print("-" * 60)
        try:
            tv_df = load_tv_signals(args.tv)
            py_df = load_python_signals(trades_df)
            result = compare(py_df, tv_df)
            passed = print_sig_report(result, len(py_df), len(tv_df))
        except Exception as e:
            print(f"  Signal comparison error: {e}")
    else:
        print("\nSTEP 4 - Signal comparison vs TradingView")
        print("-" * 60)
        print("  Skipped - no TV signals CSV provided")
        print("\n  To compare vs TradingView:")
        print("  1. Add phase2/tv_signal_exporter.pine to your chart")
        print("  2. Right-click -> Download historical data")
        print("  3. Save as phase2/data/tv_signals.csv")
        print("  4. Run: python phase2/run_phase2.py --tv phase2/data/tv_signals.csv")
        print(f"\n  Sample trades (first 5):")
        cols = ["trade_id","signal_type","entry_bar","entry_price","sl","tp","exit_reason","real_pl"]
        print(trades_df[cols].head().to_string(index=False))


if __name__ == "__main__":
    main()
