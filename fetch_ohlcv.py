"""
fetch_ohlcv.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

One-off script to generate the OHLCV CSV needed by shadow_compare.py:

    python3 -m phase2.shadow_compare --shadow shadow_log.jsonl --ohlcv btc_30m.csv --readiness

Pulls from the EXACT same source, symbol, and timeframe your bot itself uses
for indicators (feed/ws_feed.py's Binance REST loader via ccxt), so the
backtest comparison is apples-to-apples with what your live bot actually sees.

  Source:    ccxt.binance  (public REST, no API key needed)
  Symbol:    BINANCE_SYMBOL   from config.py  (default "BTC/USDT")
  Timeframe: CANDLE_TIMEFRAME from config.py  (default "30m")

Usage:
    python3 fetch_ohlcv.py                     # last 1500 bars (default)
    python3 fetch_ohlcv.py --bars 3000          # more history
    python3 fetch_ohlcv.py --out btc_30m.csv    # custom output path
    python3 fetch_ohlcv.py --since 2026-06-01   # from a specific date to now

Output: CSV with columns [timestamp, open, high, low, close, volume] —
        timestamp in epoch milliseconds, matching feed/ws_feed.py::_to_df()
        exactly, so it's a drop-in for --ohlcv.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

import pandas as pd

try:
    import ccxt
except ImportError:
    sys.exit(
        "ccxt is required (it's already a bot dependency). "
        "If this fails, run: pip3 install ccxt --break-system-packages"
    )

# Reuse the bot's own config so symbol/timeframe always match what it trades.
try:
    from config import BINANCE_SYMBOL, CANDLE_TIMEFRAME
except Exception:
    # Fallback if run outside the repo / config import fails for any reason.
    BINANCE_SYMBOL, CANDLE_TIMEFRAME = "BTC/USDT", "30m"

_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}


def _period_ms(tf: str) -> int:
    if tf not in _TF_MS:
        sys.exit(f"Unknown timeframe {tf!r} — add it to _TF_MS in this script.")
    return _TF_MS[tf]


def fetch(symbol: str, timeframe: str, total_bars: int,
          since_ms: int | None = None) -> pd.DataFrame:
    """
    Paginate ccxt.binance.fetch_ohlcv the same way feed/ws_feed.py does:
    walk backwards in time (or forwards from --since) in <=1000-bar batches.
    """
    exchange = ccxt.binance({"enableRateLimit": True})
    exchange.load_markets()
    period = _period_ms(timeframe)

    all_ohlcv: list = []

    if since_ms is not None:
        # Walk FORWARD from a fixed start date to now.
        cursor = since_ms
        while len(all_ohlcv) < total_bars:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
            if not batch:
                break
            all_ohlcv.extend(batch)
            cursor = int(batch[-1][0]) + period
            print(f"  fetched {len(all_ohlcv)} bars... "
                  f"(up to {datetime.fromtimestamp(batch[-1][0]/1000, tz=timezone.utc)})")
            if len(batch) < 1000:
                break
            time.sleep(exchange.rateLimit / 1000)
        all_ohlcv = all_ohlcv[:total_bars]
    else:
        # Walk BACKWARD from "now" — mirrors feed/ws_feed.py's historical load.
        earliest_ts = None
        while len(all_ohlcv) < total_bars:
            batch_size = min(total_bars - len(all_ohlcv), 1000)
            if earliest_ts is None:
                batch = exchange.fetch_ohlcv(symbol, timeframe, limit=batch_size)
            else:
                go_back_ms = batch_size * period
                since_ts = earliest_ts - go_back_ms
                batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ts, limit=batch_size)
            if not batch:
                break
            if earliest_ts is None:
                all_ohlcv = batch
            else:
                cutoff = earliest_ts
                older = [b for b in batch if int(b[0]) < cutoff]
                all_ohlcv = older + all_ohlcv
            earliest_ts = int(all_ohlcv[0][0])
            print(f"  fetched {len(all_ohlcv)}/{total_bars} bars...")
            if len(batch) < batch_size:
                break
        all_ohlcv = all_ohlcv[-total_bars:]

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return df.astype({"open": float, "high": float, "low": float,
                       "close": float, "volume": float})


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch OHLCV CSV for shadow_compare.py")
    ap.add_argument("--symbol", default=BINANCE_SYMBOL,
                     help=f"Binance symbol (default from config.py: {BINANCE_SYMBOL})")
    ap.add_argument("--timeframe", default=CANDLE_TIMEFRAME,
                     help=f"Candle timeframe (default from config.py: {CANDLE_TIMEFRAME})")
    ap.add_argument("--bars", type=int, default=1500,
                     help="Number of bars to fetch (default 1500)")
    ap.add_argument("--since", default=None,
                     help="Start date YYYY-MM-DD (fetch forward from here instead of "
                          "backward from now) — use this to match your shadow_log's start")
    ap.add_argument("--out", default="btc_30m.csv", help="Output CSV path")
    args = ap.parse_args()

    since_ms = None
    if args.since:
        dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        since_ms = int(dt.timestamp() * 1000)

    print(f"Fetching {args.bars} bars of {args.symbol} [{args.timeframe}] from Binance...")
    df = fetch(args.symbol, args.timeframe, args.bars, since_ms)
    df.to_csv(args.out, index=False)
    print(f"\nSaved {len(df)} bars -> {args.out}")
    if len(df):
        t0 = datetime.fromtimestamp(df.iloc[0]["timestamp"] / 1000, tz=timezone.utc)
        t1 = datetime.fromtimestamp(df.iloc[-1]["timestamp"] / 1000, tz=timezone.utc)
        print(f"  range: {t0} -> {t1}")


if __name__ == "__main__":
    main()
