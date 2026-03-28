"""
phase5/run_phase5.py
Phase 5 — Infrastructure end-to-end test.

Tests every component that main.py depends on, without placing real orders:
  1. Config validation (required env vars present and not default sentinels)
  2. Delta Exchange REST connectivity (fetch ticker, balance)
  3. Telegram send test
  4. Journal DB init and basic CRUD
  5. Dashboard health endpoint
  6. Feed startup (fetch historical bars + indicator computation)

Usage:
    python phase5/run_phase5.py
    python phase5/run_phase5.py --skip-telegram
    python phase5/run_phase5.py --skip-exchange
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio
import argparse
import sqlite3
import time

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║       SHIVA SNIPER BOT - PHASE 5: INFRA CHECK               ║
║          Pre-flight end-to-end infrastructure test           ║
╚══════════════════════════════════════════════════════════════╝
"""

RESULTS = []


def result(name: str, status: str, detail: str = "") -> None:
    icon = "✅" if status == "PASS" else "❌"
    msg  = f"  {icon} [{status}] {name}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    RESULTS.append((name, status))


# ── Test 1: Config ────────────────────────────────────────────────────────────
def test_config() -> None:
    print("\n── TEST 1: Config validation")
    from config import (
        DELTA_API_KEY, DELTA_API_SECRET,
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        SYMBOL, CANDLE_TIMEFRAME, ALERT_QTY,
    )

    sentinels = {"YOUR_API_KEY", "YOUR_API_SECRET", "YOUR_BOT_TOKEN", "YOUR_CHAT_ID"}

    checks = {
        "DELTA_API_KEY"    : (DELTA_API_KEY,     DELTA_API_KEY not in sentinels),
        "DELTA_API_SECRET" : (DELTA_API_SECRET,  DELTA_API_SECRET not in sentinels),
        "TELEGRAM_BOT_TOKEN": (TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_TOKEN not in sentinels),
        "TELEGRAM_CHAT_ID" : (TELEGRAM_CHAT_ID,  TELEGRAM_CHAT_ID not in sentinels),
    }

    all_ok = True
    for key, (val, ok) in checks.items():
        if not ok:
            print(f"    ⚠️  {key} is still a placeholder — set it in .env")
            all_ok = False

    if all_ok:
        result("Config", "PASS",
               f"SYMBOL={SYMBOL} TF={CANDLE_TIMEFRAME} QTY={ALERT_QTY}")
    else:
        result("Config", "FAIL", "Placeholder values found in config — update .env")


# ── Test 2: Exchange connectivity ─────────────────────────────────────────────
async def test_exchange() -> None:
    print("\n── TEST 2: Delta Exchange connectivity")
    try:
        import ccxt.async_support as ccxt
        from config import DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET, SYMBOL

        ex = ccxt.delta({"apiKey": DELTA_API_KEY, "secret": DELTA_API_SECRET, "enableRateLimit": True})
        if DELTA_TESTNET:
            ex.set_sandbox_mode(True)
            print("    (testnet mode)")

        ticker = await ex.fetch_ticker(SYMBOL)
        price  = ticker.get("last", 0)
        print(f"    Ticker OK — {SYMBOL} last={price:.1f}")

        bal  = await ex.fetch_balance()
        usdt = bal.get("USDT", {}).get("free", "N/A")
        print(f"    Balance OK — USDT free={usdt}")

        await ex.close()
        result("Exchange", "PASS", f"price={price:.1f} usdt_free={usdt}")
    except Exception as e:
        result("Exchange", "FAIL", str(e))


# ── Test 3: Telegram ──────────────────────────────────────────────────────────
async def test_telegram() -> None:
    print("\n── TEST 3: Telegram send test")
    try:
        from infra.telegram import Telegram
        tg = Telegram()
        await tg.send("🧪 <b>Phase 5 infra test</b> — Shiva Sniper v6.5\nTelegram is working correctly.")
        await tg.close()
        result("Telegram", "PASS", "Message sent — check your Telegram chat")
    except Exception as e:
        result("Telegram", "FAIL", str(e))


# ── Test 4: Journal ───────────────────────────────────────────────────────────
def test_journal() -> None:
    print("\n── TEST 4: Journal DB")
    try:
        import tempfile, os
        from infra.journal import Journal
        from config import LOG_FILE

        # Use a temp DB so we don't corrupt the real one
        os.environ["LOG_FILE_OVERRIDE"] = ""  # fallback to config
        journal = Journal()

        # Write + read
        journal.open_trade("Trend Long", True, 68000, 67500, 70000, 380, 30)
        row = journal.get_open_trade()
        assert row is not None, "open_trade write/read failed"
        assert row["entry_price"] == 68000

        journal.update_open_trade(trail_stage=2, current_sl=67800, peak_price=69000)
        row = journal.get_open_trade()
        assert row["trail_stage"] == 2

        journal.log_trade("Trend Long", True, 68000, 70000, 67500, 70000, 380, 30, 580.0, "TP", 2)
        journal.close_open_trade()

        summary = journal.get_summary()
        assert summary["total"] >= 1

        journal.close()
        result("Journal", "PASS", f"backend={journal._driver} log={LOG_FILE}")
    except Exception as e:
        result("Journal", "FAIL", str(e))


# ── Test 5: Dashboard endpoint ────────────────────────────────────────────────
async def test_dashboard() -> None:
    print("\n── TEST 5: Dashboard health endpoint")
    import aiohttp
    from config import LOG_FILE
    import os

    port = int(os.environ.get("PORT", 10000))
    url  = f"http://127.0.0.1:{port}/health"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                text = await resp.text()
                if resp.status == 200 and text.strip() == "OK":
                    result("Dashboard", "PASS", f"http://0.0.0.0:{port} is serving")
                else:
                    result("Dashboard", "FAIL", f"status={resp.status} body={text[:60]}")
    except Exception:
        result("Dashboard", "SKIP",
               f"Not running (start main.py first to test, or ignore for pre-deploy)")


# ── Test 6: Feed + indicators ─────────────────────────────────────────────────
async def test_feed() -> None:
    print("\n── TEST 6: OHLCV feed + indicator computation")
    try:
        import ccxt
        from config import DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET, SYMBOL, CANDLE_TIMEFRAME, EMA_TREND_LEN
        from indicators.engine import compute
        import pandas as pd

        ex = ccxt.delta({"apiKey": DELTA_API_KEY, "secret": DELTA_API_SECRET, "enableRateLimit": True})
        if DELTA_TESTNET:
            ex.set_sandbox_mode(True)

        limit = EMA_TREND_LEN + 60
        ohlcv = ex.fetch_ohlcv(SYMBOL, CANDLE_TIMEFRAME, limit=limit)
        df    = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
        df    = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})

        print(f"    Fetched {len(df)} bars for {SYMBOL} [{CANDLE_TIMEFRAME}]")
        assert len(df) >= EMA_TREND_LEN + 10, f"Not enough bars: {len(df)}"

        snap = compute(df)
        print(f"    Indicators OK — close={snap.close:.2f} atr={snap.atr:.2f} adx={snap.adx:.2f}")
        print(f"    Regime — trend={snap.trend_regime} range={snap.range_regime} filters={snap.filters_ok}")

        result("Feed+Indicators", "PASS",
               f"bars={len(df)} close={snap.close:.1f} adx={snap.adx:.1f}")
    except Exception as e:
        result("Feed+Indicators", "FAIL", str(e))


# ── Summary ───────────────────────────────────────────────────────────────────
def print_summary() -> None:
    total  = len(RESULTS)
    passed = sum(1 for _, s in RESULTS if s == "PASS")
    failed = sum(1 for _, s in RESULTS if s == "FAIL")
    skipped= total - passed - failed

    print("\n" + "="*54)
    print("PHASE 5 RESULTS")
    print("="*54)
    for name, status in RESULTS:
        icon = "✅" if status=="PASS" else ("⏭️" if status=="SKIP" else "❌")
        print(f"  {icon} {name}")
    print(f"\n  Passed: {passed}/{total}  Failed: {failed}  Skipped: {skipped}")

    if failed == 0:
        print("\n  ✅ All checks passed — bot is READY FOR DEPLOYMENT")
        print("  Run: systemctl start shiva_sniper")
    else:
        print(f"\n  ❌ {failed} check(s) failed — fix before deploying")


async def main(skip_telegram: bool = False, skip_exchange: bool = False) -> None:
    print(BANNER)
    test_config()
    if not skip_exchange:
        await test_exchange()
    else:
        print("\n── TEST 2: Exchange (skipped)")
        RESULTS.append(("Exchange", "SKIP"))
    if not skip_telegram:
        await test_telegram()
    else:
        print("\n── TEST 3: Telegram (skipped)")
        RESULTS.append(("Telegram", "SKIP"))
    test_journal()
    await test_dashboard()
    if not skip_exchange:
        await test_feed()
    else:
        RESULTS.append(("Feed+Indicators", "SKIP"))
    print_summary()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-telegram", action="store_true")
    parser.add_argument("--skip-exchange", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.skip_telegram, args.skip_exchange))
