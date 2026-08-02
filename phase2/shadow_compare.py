"""
phase2/shadow_compare.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Compare the LIVE/paper shadow log (infra/shadow_logger.py) against the backtest
to detect divergence (Trend_Breakout_Strategy_Spec.pdf §6C / §8 monitoring).

It answers the question that matters before scaling size:
    "Is the bot, running forward, actually generating the same entries the
     backtest generated — at the same bars, same direction, same levels?"

Two inputs:
  --shadow   shadow_log.jsonl      (produced live when SHADOW_LOG_ENABLED=true)
  and ONE of:
    --ohlcv  data.csv              (raw OHLCV; runs phase2 paper_engine to get
                                    the backtest entries on the same bars)
    --backtest trades.csv          (a pre-exported paper_engine.trades_to_df CSV)

Output:
  • Console summary: matched entries, live-only, backtest-only, type/dir
    mismatches, level drift, and (if exit events are present) equity curves.
  • --out divergence.csv  : per-bar divergence rows for inspection.

Alignment is by bar timestamp. A "live entry" is a shadow row with
actionable=true (signal fired AND bot was flat). A "backtest entry" is a
PaperTrade with matching entry_ts.

Usage:
    python -m phase2.shadow_compare --shadow shadow_log.jsonl --ohlcv btc_30m.csv
    python -m phase2.shadow_compare --shadow shadow_log.jsonl --backtest trades.csv --out div.csv
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── loaders ────────────────────────────────────────────────────────────────

def load_shadow(path: str) -> list[dict]:
    """Read shadow JSONL. Returns all 'bar' rows (one per confirmed bar)."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(obj)
    return rows


def backtest_from_ohlcv(csv_path: str):
    """Run the paper engine on raw OHLCV to get backtest trades."""
    import pandas as pd
    from phase2.paper_engine import run as paper_run
    df = pd.read_csv(csv_path)
    need = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"OHLCV csv missing columns: {sorted(missing)}")
    trades = paper_run(df)
    return [
        {
            "entry_ts":    int(t.entry_ts),
            "signal_type": t.signal_type,
            "is_long":     bool(t.is_long),
            "entry_price": float(t.entry_price),
            "sl":          float(t.sl),
            "tp":          float(t.tp),
            "real_pl":     float(t.real_pl),
            "exit_reason": t.exit_reason,
        }
        for t in trades
    ]


def backtest_from_csv(csv_path: str):
    """Read a pre-exported trades_to_df CSV."""
    import pandas as pd
    df = pd.read_csv(csv_path)
    out = []
    for _, r in df.iterrows():
        out.append({
            "entry_ts":    int(r["entry_ts"]),
            "signal_type": str(r["signal_type"]),
            "is_long":     bool(r["is_long"]),
            "entry_price": float(r.get("entry_price", 0.0)),
            "sl":          float(r.get("sl", 0.0)),
            "tp":          float(r.get("tp", 0.0)),
            "real_pl":     float(r.get("real_pl", 0.0)),
            "exit_reason": str(r.get("exit_reason", "")),
        })
    return out


# ── comparison ──────────────────────────────────────────────────────────────

def compare(shadow_rows: list[dict], backtest: list[dict]) -> dict:
    # live entries = actionable shadow bars, keyed by ts
    live = {}
    for r in shadow_rows:
        if r.get("event") == "bar" and r.get("actionable"):
            live[int(r.get("ts", 0))] = r
    bt = {int(t["entry_ts"]): t for t in backtest}

    live_ts = set(live)
    bt_ts   = set(bt)

    matched, mismatched, live_only, bt_only = [], [], [], []

    for ts in sorted(live_ts & bt_ts):
        lr, tr = live[ts], bt[ts]
        same_type = (lr.get("signal") == tr["signal_type"])
        same_dir  = (lr.get("is_long") == tr["is_long"])
        sl_drift  = abs(lr.get("intended_sl", 0.0) - tr["sl"])
        tp_drift  = abs(lr.get("intended_tp", 0.0) - tr["tp"])
        rec = {
            "ts": ts, "live_signal": lr.get("signal"),
            "bt_signal": tr["signal_type"],
            "sl_drift": round(sl_drift, 2), "tp_drift": round(tp_drift, 2),
        }
        if same_type and same_dir:
            matched.append(rec)
        else:
            mismatched.append(rec)

    for ts in sorted(live_ts - bt_ts):
        live_only.append({"ts": ts, "live_signal": live[ts].get("signal")})
    for ts in sorted(bt_ts - live_ts):
        bt_only.append({"ts": ts, "bt_signal": bt[ts]["signal_type"]})

    # equity curves
    bt_equity = round(sum(t["real_pl"] for t in backtest), 2)
    shadow_exits = [r for r in shadow_rows if r.get("event") == "exit"]
    shadow_equity = round(sum(float(r.get("real_pl", 0.0)) for r in shadow_exits), 2) \
        if shadow_exits else None

    # ── PER-TRADE comparison: match each shadow exit to its backtest trade ──
    # by entry_ts, so you can see live vs backtest P/L trade by trade.
    per_trade = []
    live_cum = 0.0
    bt_cum = 0.0
    for ex in sorted(shadow_exits, key=lambda r: int(r.get("entry_ts", 0))):
        ts = int(ex.get("entry_ts", 0))
        tr = bt.get(ts)
        live_pl = float(ex.get("real_pl", 0.0))
        live_cum += live_pl
        row = {
            "entry_ts":    ts,
            "dir":         ("LONG" if ex.get("is_long") else "SHORT"),
            "live_entry":  ex.get("entry_price"),
            "live_exit":   ex.get("exit_price"),
            "live_reason": ex.get("exit_reason"),
            "live_pl":     round(live_pl, 4),
            "live_cum":    round(live_cum, 4),
        }
        if tr:
            bt_cum += tr["real_pl"]
            row.update({
                "bt_entry":  round(tr["entry_price"], 2),
                "bt_exit":   round(tr.get("exit_price", 0.0), 2) if "exit_price" in tr else None,
                "bt_reason": tr.get("exit_reason", ""),
                "bt_pl":     round(tr["real_pl"], 4),
                "bt_cum":    round(bt_cum, 4),
                "pl_diff":   round(live_pl - tr["real_pl"], 4),
                "matched":   True,
            })
        else:
            row.update({"bt_entry": None, "bt_exit": None, "bt_reason": "",
                        "bt_pl": None, "bt_cum": round(bt_cum, 4),
                        "pl_diff": None, "matched": False})
        per_trade.append(row)

    return {
        "matched": matched, "mismatched": mismatched,
        "live_only": live_only, "bt_only": bt_only,
        "bt_entries": len(backtest), "live_entries": len(live),
        "bt_equity": bt_equity, "shadow_equity": shadow_equity,
        "shadow_exit_count": len(shadow_exits),
        "per_trade": per_trade,
    }


def print_report(res: dict) -> None:
    n_match = len(res["matched"]); n_mis = len(res["mismatched"])
    n_lo = len(res["live_only"]);  n_bo = len(res["bt_only"])
    total = n_match + n_mis + n_lo + n_bo
    agree = (n_match / total * 100.0) if total else 0.0

    print("=" * 66)
    print("SHADOW vs BACKTEST — ENTRY DIVERGENCE")
    print("=" * 66)
    print(f"  backtest entries : {res['bt_entries']}")
    print(f"  live entries     : {res['live_entries']}")
    print(f"  matched (ts+type+dir) : {n_match}")
    print(f"  mismatched type/dir   : {n_mis}")
    print(f"  live-only (bot fired, backtest didn't)   : {n_lo}")
    print(f"  backtest-only (backtest fired, bot didn't): {n_bo}")
    print(f"  agreement rate   : {agree:.1f}%")
    if res["mismatched"]:
        print("\n  --- mismatches (first 10) ---")
        for r in res["mismatched"][:10]:
            print(f"    ts={r['ts']}  live={r['live_signal']}  bt={r['bt_signal']}")
    if res["live_only"]:
        print("\n  --- live-only (first 10) — GHOST entries, investigate feed/timing ---")
        for r in res["live_only"][:10]:
            print(f"    ts={r['ts']}  {r['live_signal']}")
    if res["bt_only"]:
        print("\n  --- backtest-only (first 10) — MISSED entries ---")
        for r in res["bt_only"][:10]:
            print(f"    ts={r['ts']}  {r['bt_signal']}")
    # level drift on matched
    drifts = [r["sl_drift"] for r in res["matched"] if "sl_drift" in r]
    if drifts:
        print(f"\n  level drift on matched: max SL drift={max(drifts):.2f} pts")
    print("\n  --- equity (shadow accounting) ---")
    print(f"    backtest net P/L : {res['bt_equity']}")
    if res["shadow_equity"] is not None:
        diff = round(res["shadow_equity"] - res["bt_equity"], 2)
        print(f"    shadow net P/L   : {res['shadow_equity']}  "
              f"(exits logged: {res['shadow_exit_count']})")
        print(f"    divergence       : {diff}")
    else:
        print("    shadow net P/L   : n/a (no 'exit' events logged yet)")

    # ── per-trade table ──
    pt = res.get("per_trade", [])
    if pt:
        print("\n  --- PER-TRADE (live vs backtest) ---")
        print(f"    {'entry_ts':>13} {'dir':>5} {'live_pl':>10} {'bt_pl':>10} "
              f"{'diff':>9} {'live_reason':>12} {'bt_reason':>10}")
        for r in pt[:40]:
            bt_pl = "n/a" if r["bt_pl"] is None else f"{r['bt_pl']:.2f}"
            diff  = "n/a" if r["pl_diff"] is None else f"{r['pl_diff']:+.2f}"
            print(f"    {r['entry_ts']:>13} {r['dir']:>5} {r['live_pl']:>10.2f} "
                  f"{bt_pl:>10} {diff:>9} {str(r['live_reason']):>12} "
                  f"{str(r['bt_reason']):>10}")
        if len(pt) > 40:
            print(f"    ... {len(pt)-40} more (see --trades csv)")
        n_unmatched = sum(1 for r in pt if not r["matched"])
        if n_unmatched:
            print(f"    NOTE: {n_unmatched} live trade(s) had no backtest match "
                  f"(entry_ts not in backtest) — shown with bt_pl=n/a")
    print("=" * 66)


def write_csv(res: dict, path: str) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "ts", "live_signal", "bt_signal", "sl_drift", "tp_drift"])
        for r in res["matched"]:
            w.writerow(["matched", r["ts"], r["live_signal"], r["bt_signal"],
                        r.get("sl_drift", ""), r.get("tp_drift", "")])
        for r in res["mismatched"]:
            w.writerow(["mismatch", r["ts"], r["live_signal"], r["bt_signal"],
                        r.get("sl_drift", ""), r.get("tp_drift", "")])
        for r in res["live_only"]:
            w.writerow(["live_only", r["ts"], r["live_signal"], "", "", ""])
        for r in res["bt_only"]:
            w.writerow(["bt_only", r["ts"], "", r["bt_signal"], "", ""])
    print(f"  wrote {path}")


def write_trades_csv(res: dict, path: str) -> None:
    import csv
    pt = res.get("per_trade", [])
    cols = ["entry_ts", "dir", "matched", "live_entry", "live_exit", "live_reason",
            "live_pl", "live_cum", "bt_entry", "bt_exit", "bt_reason", "bt_pl",
            "bt_cum", "pl_diff"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in pt:
            w.writerow({c: r.get(c) for c in cols})
    print(f"  wrote {path}  ({len(pt)} trades)")


def _stats(pls: list[float]) -> dict:
    """net P/L, count, win rate, profit factor for a list of trade P/Ls."""
    n = len(pls)
    if n == 0:
        return {"net": 0.0, "n": 0, "win_rate": 0.0, "profit_factor": 0.0}
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "net": round(sum(pls), 2),
        "n": n,
        "win_rate": round(len(wins) / n * 100.0, 1),
        "profit_factor": (round(pf, 2) if pf != float("inf") else pf),
    }


def readiness_report(res: dict, backtest: list[dict]) -> None:
    """
    Print a GREEN/RED verdict on (1) does live TRACK the backtest, and
    (2) is it PROFITABLE so far — with an honest sample-size caveat.
    """
    n_match = len(res["matched"]); n_mis = len(res["mismatched"])
    n_lo = len(res["live_only"]);  n_bo = len(res["bt_only"])
    total = n_match + n_mis + n_lo + n_bo
    agree = (n_match / total * 100.0) if total else 0.0
    drifts = [r.get("sl_drift", 0.0) for r in res["matched"]]
    max_drift = max(drifts) if drifts else 0.0

    pt = res.get("per_trade", [])
    live_pls = [r["live_pl"] for r in pt]
    bt_pls   = [t["real_pl"] for t in backtest]
    diffs    = [r["pl_diff"] for r in pt if r.get("pl_diff") is not None]
    avg_diff = (sum(diffs) / len(diffs)) if diffs else 0.0

    live = _stats(live_pls)
    bt   = _stats(bt_pls)

    def mark(ok): return "GREEN ✅" if ok else "RED ❌"

    # ── PART 1: does live track the backtest? ──
    c1 = agree >= 95.0
    c2 = n_lo == 0
    c3 = n_bo == 0
    c4 = max_drift < 5.0
    c5 = abs(avg_diff) < 15.0   # avg per-trade gap small (slippage only)

    print("=" * 66)
    print("READINESS — DOES LIVE MATCH THE BACKTEST?")
    print("=" * 66)
    print(f"  {mark(c1)}  agreement >= 95%          : {agree:.1f}%")
    print(f"  {mark(c2)}  no ghost entries          : {n_lo}")
    print(f"  {mark(c3)}  no missed entries         : {n_bo}")
    print(f"  {mark(c4)}  SL/TP drift < 5 pts       : {max_drift:.2f}")
    print(f"  {mark(c5)}  avg per-trade gap < 15    : {avg_diff:+.2f}")
    tracking_ok = all([c1, c2, c3, c4, c5])
    print(f"\n  TRACKING VERDICT: {mark(tracking_ok)}  "
          f"({'live matches backtest' if tracking_ok else 'live diverges — investigate'})")

    # ── PART 2: profitability so far ──
    print("\n" + "-" * 66)
    print("PROFITABILITY (live so far  vs  backtest)")
    print("-" * 66)
    print(f"  {'':18}{'LIVE':>14}{'BACKTEST':>14}")
    print(f"  {'trades':18}{live['n']:>14}{bt['n']:>14}")
    print(f"  {'net P/L':18}{live['net']:>14}{bt['net']:>14}")
    print(f"  {'win rate %':18}{live['win_rate']:>14}{bt['win_rate']:>14}")
    print(f"  {'profit factor':18}{str(live['profit_factor']):>14}{str(bt['profit_factor']):>14}")

    live_profitable = live["net"] > 0 and live["profit_factor"] != float("inf") \
        and live["profit_factor"] > 1.0 or (live["net"] > 0)
    close = "n/a"
    if bt["net"] != 0:
        close_pct = (live["net"] - bt["net"]) / abs(bt["net"]) * 100.0
        close = f"{close_pct:+.1f}% vs backtest"
    print(f"\n  live net P/L is {'POSITIVE' if live['net'] > 0 else 'NEGATIVE'} "
          f"({close})")

    # ── sample-size honesty ──
    print("\n" + "-" * 66)
    if live["n"] < 300:
        print("  ⚠️  SAMPLE TOO SMALL FOR A PROFIT VERDICT")
        print(f"     {live['n']} live trades so far. Need 300+ (≈60 days) before")
        print("     'profitable' means anything — a short run can be luck either way.")
        print("     What you CAN conclude now: whether the bot TRACKS the backtest")
        print("     (Part 1 above). Profit confirmation comes later.")
    else:
        print("  Sample size OK (300+ trades). Profit numbers are meaningful,")
        print("  but live friction (slippage) means live <= backtest is normal.")
    print("=" * 66)


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare shadow log vs backtest entries.")
    ap.add_argument("--shadow", required=True, help="shadow_log.jsonl from live/paper run")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ohlcv", help="raw OHLCV csv → runs paper_engine for backtest")
    g.add_argument("--backtest", help="pre-exported trades_to_df csv")
    ap.add_argument("--out", help="optional entry-divergence csv path")
    ap.add_argument("--trades", help="optional per-trade comparison csv path")
    ap.add_argument("--readiness", action="store_true",
                    help="print GREEN/RED tracking + profitability verdict")
    args = ap.parse_args()

    shadow_rows = load_shadow(args.shadow)
    backtest = backtest_from_ohlcv(args.ohlcv) if args.ohlcv \
        else backtest_from_csv(args.backtest)

    res = compare(shadow_rows, backtest)
    print_report(res)
    if args.readiness:
        readiness_report(res, backtest)
    if args.out:
        write_csv(res, args.out)
    if args.trades:
        write_trades_csv(res, args.trades)


if __name__ == "__main__":
    main()
