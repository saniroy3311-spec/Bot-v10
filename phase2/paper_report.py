"""
phase2/paper_report.py
Generate full performance report from paper trading results.
Compares key metrics against TradingView Strategy Report.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
from tabulate import tabulate


def generate(trades_df: pd.DataFrame,
             initial_capital: float = 10000.0) -> dict:
    """
    Compute performance metrics from completed trades.
    Returns dict of metrics + formatted report string.
    """
    if trades_df.empty:
        return {"error": "No trades to report"}

    pl     = trades_df["real_pl"]
    wins   = pl[pl > 0]
    losses = pl[pl < 0]

    total_pl       = pl.sum()
    win_rate       = len(wins) / len(pl) * 100
    avg_win        = wins.mean() if len(wins) else 0
    avg_loss       = losses.mean() if len(losses) else 0
    profit_factor  = (wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else float("inf")

    # Equity curve + drawdown
    equity         = initial_capital + pl.cumsum()
    peak           = equity.cummax()
    drawdown       = (equity - peak) / peak * 100
    max_drawdown   = drawdown.min()

    # Exit reason breakdown
    reasons        = trades_df["exit_reason"].value_counts().to_dict()

    # Trail stage at exit
    stage_dist     = trades_df["trail_stage"].value_counts().sort_index().to_dict()

    metrics = {
        "total_trades"  : len(trades_df),
        "total_pl"      : round(total_pl, 2),
        "pct_return"    : round(total_pl / initial_capital * 100, 2),
        "win_rate"      : round(win_rate, 2),
        "profit_factor" : round(profit_factor, 3),
        "avg_win"       : round(avg_win, 2),
        "avg_loss"      : round(avg_loss, 2),
        "max_drawdown"  : round(max_drawdown, 2),
        "exit_reasons"  : reasons,
        "trail_stages"  : stage_dist,
        "avg_bars_held" : round(trades_df["bars_held"].mean(), 1),
    }
    return metrics


def print_report(metrics: dict, tv_metrics: dict = None) -> None:
    """
    Print formatted performance report.
    If tv_metrics provided, shows side-by-side comparison.
    """
    print(f"\n{'═'*65}")
    print("PHASE 2 — PAPER TRADING PERFORMANCE REPORT")
    print(f"{'═'*65}\n")

    rows = [
        ["Total trades",   metrics["total_trades"],
         tv_metrics.get("total_trades", "—") if tv_metrics else "—"],
        ["Total P/L",      f"${metrics['total_pl']:+.2f}",
         f"${tv_metrics.get('total_pl', 0):+.2f}" if tv_metrics else "—"],
        ["Return %",       f"{metrics['pct_return']:+.2f}%",
         f"{tv_metrics.get('pct_return', 0):+.2f}%" if tv_metrics else "—"],
        ["Win rate",       f"{metrics['win_rate']:.1f}%",
         f"{tv_metrics.get('win_rate', 0):.1f}%" if tv_metrics else "—"],
        ["Profit factor",  f"{metrics['profit_factor']:.3f}",
         f"{tv_metrics.get('profit_factor', 0):.3f}" if tv_metrics else "—"],
        ["Avg win",        f"${metrics['avg_win']:.2f}",
         f"${tv_metrics.get('avg_win', 0):.2f}" if tv_metrics else "—"],
        ["Avg loss",       f"${metrics['avg_loss']:.2f}",
         f"${tv_metrics.get('avg_loss', 0):.2f}" if tv_metrics else "—"],
        ["Max drawdown",   f"{metrics['max_drawdown']:.2f}%",
         f"{tv_metrics.get('max_drawdown', 0):.2f}%" if tv_metrics else "—"],
        ["Avg bars held",  metrics["avg_bars_held"], "—"],
    ]

    headers = ["Metric", "Python Bot", "TradingView"]
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))

    # Exit reason breakdown
    print(f"\nExit reason breakdown:")
    for reason, count in sorted(metrics["exit_reasons"].items(),
                                key=lambda x: -x[1]):
        pct = count / metrics["total_trades"] * 100
        print(f"  {reason:<20} {count:>4} trades  ({pct:.1f}%)")

    # Trail stage at exit
    print(f"\nTrail stage at exit:")
    for stage, count in sorted(metrics["trail_stages"].items()):
        label = f"Stage {stage}" if stage > 0 else "No trail"
        pct   = count / metrics["total_trades"] * 100
        print(f"  {label:<15} {count:>4} trades  ({pct:.1f}%)")

    # Match quality vs TV
    if tv_metrics:
        print(f"\n{'─'*65}")
        print("Match quality vs TradingView:")
        pl_gap = abs(metrics["total_pl"] - tv_metrics.get("total_pl", 0))
        pl_pct = pl_gap / abs(tv_metrics.get("total_pl", 1)) * 100
        wl_gap = abs(metrics["win_rate"] - tv_metrics.get("win_rate", 0))
        pf_gap = abs(metrics["profit_factor"] - tv_metrics.get("profit_factor", 0))
        print(f"  P/L gap        : ${pl_gap:.2f} ({pl_pct:.1f}%)")
        print(f"  Win rate gap   : {wl_gap:.1f}%")
        print(f"  Profit factor gap : {pf_gap:.3f}")

        if pl_pct < 5 and wl_gap < 2:
            print("\n  ✅ Match quality: EXCELLENT (< 5% P/L gap)")
        elif pl_pct < 10:
            print("\n  🟡 Match quality: GOOD (5–10% P/L gap)")
        else:
            print("\n  ❌ Match quality: POOR (> 10% gap — investigate)")
