"""
phase1/run_phase1.py
ONE COMMAND — runs entire Phase 1:
  Step 1: Fetch real BTCUSDT OHLCV from Binance
  Step 2: Compute all indicators (Python engine)
  Step 3: Self-test (internal consistency check)
  Step 4: Print instructions for TV comparison

Usage:
    cd shiva_sniper_bot
    python phase1/run_phase1.py
    python phase1/run_phase1.py --tf 4h --bars 500
    python phase1/run_phase1.py --tv phase1/data/tv_export.csv  # full comparison
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
from phase1.fetch_ohlcv       import fetch
from phase1.compute_indicators import run as compute_run
from phase1.verify             import run as verify_run


BANNER = """
╔══════════════════════════════════════════════════════════════╗
║        SHIVA SNIPER BOT — PHASE 1: FEED + INDICATORS        ║
║              Verifying values match TradingView              ║
╚══════════════════════════════════════════════════════════════╝
"""


def main():
    print(BANNER)

    parser = argparse.ArgumentParser()
    parser.add_argument("--tf",   default="1h",  help="Timeframe (default: 1h)")
    parser.add_argument("--bars", default=500, type=int, help="Bars to fetch")
    parser.add_argument("--tv",   default=None,  help="TV export CSV path (optional)")
    args = parser.parse_args()

    # ── Step 1: Fetch ──────────────────────────────────────────────
    print("STEP 1 — Fetching OHLCV data from Binance")
    print("─" * 60)
    df, csv_path = fetch(args.tf, args.bars)

    # ── Step 2: Compute ────────────────────────────────────────────
    print("\nSTEP 2 — Computing indicators (Python engine)")
    print("─" * 60)
    indicators_csv = compute_run(csv_path)

    # ── Step 3: Verify ─────────────────────────────────────────────
    print("\nSTEP 3 — Verification")
    print("─" * 60)
    verify_run(py_csv=indicators_csv, tv_csv=args.tv)

    # ── Step 4: TV instructions (if no TV CSV provided) ────────────
    if not args.tv:
        print("\n" + "═"*60)
        print("STEP 4 — Compare against TradingView (manual)")
        print("═"*60)
        print("""
To complete full TV verification:

1. Open TradingView → BTCUSDT {tf} chart
2. Add script: phase1/tv_exporter.pine
3. Right-click chart → "Download historical data"
4. Save file as:  phase1/data/tv_export.csv
5. Run:
       python phase1/run_phase1.py --tv phase1/data/tv_export.csv

Pass criteria:
   ✅ All indicators: Δ < 0.01%
   🟡 Warning:        Δ 0.01% – 0.05%
   ❌ Fail:           Δ > 0.05%  ← must fix before Phase 2
""".format(tf=args.tf))


if __name__ == "__main__":
    main()
