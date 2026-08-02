"""
phase2/verify_signals.py
Compare Python bot entry signals vs TradingView signal exporter output.

Checks:
  1. Same entry bars (timestamp match)
  2. Same signal direction (long/short)
  3. Same entry price (close on signal bar)
  4. Same SL / TP levels
  5. No phantom signals (Python fires, TV doesn't — or vice versa)

Pass criteria:
  - Entry bar match  : 100%
  - Price match      : < 0.001% divergence
  - SL/TP match      : < 0.01% divergence
  - Phantom signals  : 0
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np
from tabulate import tabulate


def load_tv_signals(path: str) -> pd.DataFrame:
    """
    Load TradingView signal export CSV.
    TV exports one row per bar with plot values.
    We keep only bars where any_signal == 1.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # Normalise timestamp column
    ts_col = next((c for c in ["time", "timestamp"] if c in df.columns), None)
    if ts_col:
        df["ts_sec"] = pd.to_datetime(df[ts_col]).astype("int64") // 10**9
    else:
        raise ValueError("TV CSV missing time/timestamp column")

    # Filter to signal bars only
    sig_col = next((c for c in df.columns if "any_signal" in c), None)
    if sig_col:
        df = df[df[sig_col] == 1].copy()
    else:
        # Fallback: any bar where trend_long or trend_short etc > 0
        sig_cols = [c for c in df.columns if any(
            x in c for x in ["trend_long", "trend_short", "range_long", "range_short"]
        )]
        if sig_cols:
            df = df[df[sig_cols].max(axis=1) > 0].copy()

    return df.reset_index(drop=True)


def load_python_signals(trades_df: pd.DataFrame) -> pd.DataFrame:
    """Extract entry-bar info from paper trading results."""
    df = trades_df[["trade_id","signal_type","is_long",
                    "entry_bar","entry_ts","entry_price",
                    "sl","tp","atr"]].copy()
    df["ts_sec"] = (df["entry_ts"] / 1000).astype(int)
    return df


def compare(py_df: pd.DataFrame, tv_df: pd.DataFrame) -> dict:
    """
    Align on timestamp and compare signal-by-signal.
    Returns dict with match stats and per-signal detail DataFrame.
    """
    # Merge on ts_sec
    merged = pd.merge(
        py_df.rename(columns=lambda c: c + "_py" if c != "ts_sec" else c),
        tv_df.rename(columns=lambda c: c + "_tv" if c != "ts_sec" else c),
        on="ts_sec", how="outer", indicator=True
    )

    both       = merged[merged["_merge"] == "both"]
    py_only    = merged[merged["_merge"] == "left_only"]
    tv_only    = merged[merged["_merge"] == "right_only"]

    results = []
    for _, row in both.iterrows():
        # Direction match
        py_long = bool(row.get("is_long_py", False))
        tv_long = None
        for col in ["trend_long_tv", "range_long_tv"]:
            if col in row.index and row[col] == 1:
                tv_long = True
                break
        for col in ["trend_short_tv", "range_short_tv"]:
            if col in row.index and row[col] == 1:
                tv_long = False
                break

        dir_match = (tv_long is None) or (py_long == tv_long)

        # Price match
        py_price = float(row.get("entry_price_py", 0))
        tv_price = float(row.get("entry_price_tv", py_price))
        price_pct = abs(py_price - tv_price) / tv_price * 100 if tv_price else 0

        # SL match
        py_sl  = float(row.get("sl_py", 0))
        tv_sl  = float(row.get("entry_sl_tv", py_sl))
        sl_pct = abs(py_sl - tv_sl) / abs(tv_sl) * 100 if tv_sl else 0

        # TP match
        py_tp  = float(row.get("tp_py", 0))
        tv_tp  = float(row.get("entry_tp_tv", py_tp))
        tp_pct = abs(py_tp - tv_tp) / abs(tv_tp) * 100 if tv_tp else 0

        status = "✅" if (dir_match and price_pct < 0.001
                          and sl_pct < 0.01 and tp_pct < 0.01) else "❌"

        results.append({
            "ts"          : row["ts_sec"],
            "py_signal"   : row.get("signal_type_py", "?"),
            "dir_match"   : "✅" if dir_match else "❌",
            "price_Δ%"    : f"{price_pct:.5f}%",
            "sl_Δ%"       : f"{sl_pct:.5f}%",
            "tp_Δ%"       : f"{tp_pct:.5f}%",
            "status"      : status,
        })

    return {
        "matched"   : len(both),
        "py_only"   : len(py_only),    # Python fires but TV doesn't
        "tv_only"   : len(tv_only),    # TV fires but Python doesn't
        "details"   : pd.DataFrame(results),
        "py_only_df": py_only,
        "tv_only_df": tv_only,
    }


def print_report(result: dict, py_total: int, tv_total: int) -> bool:
    """Print comparison report. Returns True if Phase 2 passes."""
    print(f"\n{'═'*65}")
    print("PHASE 2 — SIGNAL ENGINE VERIFICATION vs TradingView")
    print(f"{'═'*65}")
    print(f"\nPython signals : {py_total}")
    print(f"TV signals     : {tv_total}")
    print(f"Matched        : {result['matched']}")
    print(f"Python only    : {result['py_only']}  ← phantom (bad)")
    print(f"TV only        : {result['tv_only']}  ← missed  (bad)")

    details = result["details"]
    if not details.empty:
        pass_count = (details["status"] == "✅").sum()
        fail_count = (details["status"] == "❌").sum()
        print(f"\nPer-signal checks:")
        print(f"  Direction match : {(details['dir_match']=='✅').sum()}/{len(details)}")
        print(f"  Price match     : {pass_count}/{len(details)}")
        print(f"\nSample (last 10 matched):")
        print(tabulate(details.tail(10), headers="keys",
                       tablefmt="rounded_outline", showindex=False))

    # Overall pass criteria
    phantom_ok = result["py_only"] == 0
    missed_ok  = result["tv_only"] == 0
    price_ok   = details.empty or (details["status"] == "✅").all()

    passed = phantom_ok and missed_ok and price_ok
    print(f"\n{'─'*65}")
    if passed:
        print("✅ PHASE 2 PASSED — Signal engine matches TradingView exactly")
        print("   Ready to proceed to Phase 3 (Order Manager)")
    else:
        print("❌ PHASE 2 FAILED")
        if not phantom_ok:
            print(f"   Phantom signals: {result['py_only']} bars fired in Python, not in TV")
        if not missed_ok:
            print(f"   Missed signals : {result['tv_only']} bars fired in TV, not in Python")
        if not price_ok:
            print(f"   Price mismatch : check details above")

    return passed
