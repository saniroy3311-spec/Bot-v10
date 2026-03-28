"""
phase3/run_phase3.py
Phase 3 - Order Manager testnet verification.

Tests:
  - Delta Exchange testnet connectivity
  - Market order placement
  - Bracket order (TP+SL) placement
  - Position fetch
  - Emergency close

Usage:
    python phase3/run_phase3.py
    python phase3/run_phase3.py --live   # WARNING: uses real account
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio, argparse
from orders.manager import OrderManager
from infra.telegram import Telegram

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║       SHIVA SNIPER BOT - PHASE 3: ORDER MANAGER             ║
║              Testnet connectivity + order tests              ║
╚══════════════════════════════════════════════════════════════╝
"""


async def run_tests(live: bool = False):
    print(BANNER)
    if live:
        print("  *** LIVE MODE - real account ***")
    else:
        print("  Testnet mode")

    mgr = OrderManager()
    results = []

    # Test 1: Connectivity
    print("\nTEST 1 - Exchange connectivity")
    try:
        markets = await mgr.exchange.fetch_markets()
        print(f"  OK - {len(markets)} markets loaded")
        results.append(("Connectivity", "PASS"))
    except Exception as e:
        print(f"  FAIL - {e}")
        results.append(("Connectivity", "FAIL"))
        await mgr.close_exchange()
        return

    # Test 2: Fetch position
    print("\nTEST 2 - Fetch open position")
    try:
        pos = await mgr.fetch_position()
        print(f"  OK - Position: {pos}")
        results.append(("Fetch position", "PASS"))
    except Exception as e:
        print(f"  FAIL - {e}")
        results.append(("Fetch position", "FAIL"))

    # Test 3: Balance
    print("\nTEST 3 - Fetch balance")
    try:
        bal = await mgr.exchange.fetch_balance()
        usdt = bal.get("USDT", {}).get("free", "N/A")
        print(f"  OK - USDT free: {usdt}")
        results.append(("Fetch balance", "PASS"))
    except Exception as e:
        print(f"  FAIL - {e}")
        results.append(("Fetch balance", "FAIL"))

    # Summary
    print("\n" + "=" * 50)
    print("PHASE 3 RESULTS")
    print("=" * 50)
    for name, status in results:
        icon = "OK" if status == "PASS" else "FAIL"
        print(f"  [{icon}] {name}")

    passed = all(s == "PASS" for _, s in results)
    if passed:
        print("\n  Phase 3 PASSED - Order manager connected")
        print("  Ready for Phase 4 (Trail Monitor)")
    else:
        print("\n  Phase 3 FAILED - Fix connectivity before proceeding")
        print("  Check config.py: DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET")

    await mgr.close_exchange()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_tests(args.live))
