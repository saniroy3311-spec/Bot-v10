"""
phase1/fetch_ohlcv.py
Fetch real BTCUSD OHLCV from Binance (no API key needed).
Falls back to deterministic synthetic data if network unavailable.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import argparse
import numpy as np
import ccxt
import pandas as pd
from datetime import datetime

SYMBOL_BN  = "BTC/USDT"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "data")

def _synthetic_ohlcv(bars):
    np.random.seed(42)
    ts, step, close, out = 1700000000000, 3600000, 50000.0, []
    for i in range(bars):
        change = np.random.randn() * 200
        open_  = close
        close  = max(1000.0, close + change)
        high   = max(open_, close) + abs(np.random.randn() * 100)
        low    = min(open_, close) - abs(np.random.randn() * 100)
        vol    = abs(np.random.randn() * 500) + 200
        out.append([ts + i * step, open_, high, low, close, vol])
    return out

def fetch(timeframe="1h", bars=500):
    print(f"Fetching {bars} bars of {SYMBOL_BN} {timeframe} from Binance...")
    synthetic = False
    try:
        exchange = ccxt.binance({"enableRateLimit": True})
        ohlcv    = exchange.fetch_ohlcv(SYMBOL_BN, timeframe, limit=bars)
        print(f"  Live data fetched ({len(ohlcv)} bars)")
    except Exception as e:
        print(f"  Network unavailable ({type(e).__name__}) — using synthetic data")
        ohlcv, synthetic = _synthetic_ohlcv(bars), True

    df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    suffix = "synthetic" if synthetic else "live"
    fname  = os.path.join(OUTPUT_DIR, f"BTCUSDT_{timeframe}_{bars}bars_{suffix}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv")
    df.to_csv(fname, index=False)
    print(f"  Saved {len(df)} bars -> {fname}")
    print(f"  Range: {df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]}")
    return df, fname

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf",   default="1h")
    parser.add_argument("--bars", default=500, type=int)
    args = parser.parse_args()
    fetch(args.tf, args.bars)
