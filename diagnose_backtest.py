"""
diagnose_backtest.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Answers "why did the backtest lose money over this window?" using your own
real OHLCV data — not a guess. Runs the same paper_engine backtest shadow_compare
uses, then breaks down:

  1. EXIT REASON breakdown — how trades actually lost/won (real SL vs
     Trail/BE SL vs TP vs Max SL), so you can see if it's death-by-fakeout
     (lots of small Trail/BE SL losses right after entry) vs something else.
  2. MARKET CHARACTER over the window — % of bars in trend regime vs range,
     average ADX, and a "chop score" (net directional move vs total absolute
     movement). Trend-breakout strategies are structurally weakest in choppy,
     range-bound markets — this tells you if that's what happened.
  3. Trades table sorted by real_pl, worst first.

Usage:
    python3 diagnose_backtest.py --ohlcv btc_30m.csv
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose why a backtest window won/lost.")
    ap.add_argument("--ohlcv", required=True, help="OHLCV csv (e.g. btc_30m.csv)")
    args = ap.parse_args()

    df = pd.read_csv(args.ohlcv)
    need = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        sys.exit(f"OHLCV csv missing columns: {sorted(missing)}")

    from indicators.engine import compute_full_series
    from phase2.paper_engine import run, trades_to_df

    series = compute_full_series(df)
    trades = run(df)

    print("=" * 66)
    print(f"DIAGNOSIS — {args.ohlcv}  ({len(df)} bars)")
    print("=" * 66)

    t0 = pd.to_datetime(df["timestamp"].iloc[0], unit="ms", utc=True)
    t1 = pd.to_datetime(df["timestamp"].iloc[-1], unit="ms", utc=True)
    print(f"  window: {t0} -> {t1}")
    print(f"  total trades: {len(trades)}")

    if not trades:
        print("  No trades in this window — nothing to diagnose.")
        return

    # ── 1. EXIT REASON BREAKDOWN ──
    tdf = trades_to_df(trades)
    print("\n" + "-" * 66)
    print("1. EXIT REASON BREAKDOWN")
    print("-" * 66)
    grp = tdf.groupby("exit_reason")["real_pl"].agg(["count", "sum", "mean"]).round(2)
    grp = grp.sort_values("sum")
    print(f"  {'reason':<16}{'count':>7}{'total_pl':>12}{'avg_pl':>10}")
    for reason, row in grp.iterrows():
        print(f"  {reason:<16}{int(row['count']):>7}{row['sum']:>12}{row['mean']:>10}")

    # quick read: fakeout-heavy?
    fakeout_losers = tdf[(tdf["exit_reason"].str.contains("Trail/BE", na=False)) & (tdf["real_pl"] < 0)]
    real_sl_losers = tdf[(tdf["exit_reason"] == "SL") & (tdf["real_pl"] < 0)]
    print(f"\n  Trail/BE SL losers (entered, small give-back): {len(fakeout_losers)} "
          f"trades, {fakeout_losers['real_pl'].sum():.2f} total")
    print(f"  Full SL losers (hit stop outright):            {len(real_sl_losers)} "
          f"trades, {real_sl_losers['real_pl'].sum():.2f} total")

    # ── 2. MARKET CHARACTER ──
    print("\n" + "-" * 66)
    print("2. MARKET CHARACTER OVER THIS WINDOW")
    print("-" * 66)
    adx_mean = series["adx"].mean()
    trend_pct = (series["adx"] > 22).mean() * 100
    range_pct = (series["adx"] < 18).mean() * 100
    mid_pct = 100 - trend_pct - range_pct

    net_move = abs(df["close"].iloc[-1] - df["close"].iloc[0])
    total_abs_move = df["close"].diff().abs().sum()
    chop_score = 1 - (net_move / total_abs_move) if total_abs_move > 0 else 0

    print(f"  average ADX:              {adx_mean:.1f}")
    print(f"  bars in trend regime (ADX>22): {trend_pct:.1f}%")
    print(f"  bars in range regime (ADX<18): {range_pct:.1f}%")
    print(f"  bars in between:               {mid_pct:.1f}%")
    print(f"  net price move over window:    {net_move:.0f} pts")
    print(f"  total absolute movement:       {total_abs_move:.0f} pts")
    print(f"  chop score (0=trending cleanly, 1=pure back-and-forth): {chop_score:.2f}")
    if chop_score > 0.9:
        print("  -> Market moved back and forth A LOT relative to its net move.")
        print("     This is the regime trend-breakout strategies struggle most in:")
        print("     ADX can spike >22 on sharp counter-swings, triggering breakout")
        print("     entries that immediately reverse.")
    elif chop_score < 0.7:
        print("  -> Market had a relatively clean net directional move.")
        print("     Losses here are less explained by chop — look at exit reasons above.")
    else:
        print("  -> Moderate chop. Mixed picture — check exit reasons above.")

    # ── 3. WORST TRADES TABLE ──
    print("\n" + "-" * 66)
    print("3. WORST TRADES (sorted, worst first)")
    print("-" * 66)
    worst = tdf.sort_values("real_pl").head(10)
    print(worst[["entry_ts", "signal_type", "entry_price", "exit_price",
                 "exit_reason", "bars_held", "real_pl"]].to_string(index=False))
    print("=" * 66)


if __name__ == "__main__":
    main()
