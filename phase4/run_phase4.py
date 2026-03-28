"""
phase4/run_phase4.py
Phase 4 - Trail Monitor live test.

Runs bot in paper mode against live price feed.
Verifies trail/BE/maxSL logic fires correctly.
Logs all SL modifications to phase4/data/trail_log.csv.

Usage:
    python phase4/run_phase4.py --bars 50   # Watch 50 bars then exit
    python phase4/run_phase4.py             # Run until Ctrl+C
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asyncio, argparse, signal as sys_signal
import pandas as pd
from datetime import datetime

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║       SHIVA SNIPER BOT - PHASE 4: TRAIL MONITOR             ║
║         Live paper trade - watch trail/BE/maxSL              ║
╚══════════════════════════════════════════════════════════════╝
"""

trail_events = []


async def run(max_bars: int = 0):
    from feed.ws_feed      import CandleFeed
    from indicators.engine import compute
    from strategy.signal   import evaluate, SignalType
    from risk.calculator   import calc_levels, TrailState, calc_trail_stage, get_trail_params, should_trigger_be
    from infra.telegram    import Telegram

    print(BANNER)
    telegram  = Telegram()
    bar_count = [0]
    position  = [None]   # {"risk": RiskLevels, "state": TrailState, "entry_bar": int}

    async def on_bar(df):
        bar_count[0] += 1
        try:
            snap = compute(df)
        except ValueError:
            return

        if position[0] is None:
            sig = evaluate(snap, False)
            if sig.signal_type != SignalType.NONE:
                risk = calc_levels(snap.close, snap.atr, sig.is_long, sig.regime == "trend")
                position[0] = {
                    "risk"      : risk,
                    "state"     : TrailState(current_sl=risk.sl),
                    "entry_bar" : bar_count[0],
                }
                print(f"\n[BAR {bar_count[0]}] ENTRY: {sig.signal_type.value}")
                print(f"  Price={snap.close:.2f}  SL={risk.sl:.2f}  TP={risk.tp:.2f}")
                await telegram.send(f"[Phase4 Paper] {sig.signal_type.value} @ {snap.close:.2f}")
        else:
            pos   = position[0]
            risk  = pos["risk"]
            state = pos["state"]
            profit_dist = (snap.close - risk.entry_price) if risk.is_long \
                          else (risk.entry_price - snap.close)

            new_stage = calc_trail_stage(profit_dist, risk.atr)  # FIX #6: use stored ATR
            if new_stage > state.stage:
                state.stage = new_stage
                print(f"  [BAR {bar_count[0]}] Trail stage -> {new_stage}")
                trail_events.append({"bar": bar_count[0], "event": f"stage_{new_stage}", "price": snap.close})

            if not state.be_done and should_trigger_be(profit_dist, risk.atr):  # FIX #6: use stored ATR
                state.current_sl = risk.entry_price
                state.be_done    = True
                print(f"  [BAR {bar_count[0]}] BREAKEVEN triggered. SL -> {state.current_sl:.2f}")
                trail_events.append({"bar": bar_count[0], "event": "breakeven", "price": snap.close})

            if state.stage > 0:
                atr = risk.atr  # FIX #6: use stored ATR from RiskLevels directly
                from risk.calculator import get_trail_params
                pts, _ = get_trail_params(state.stage, atr)  # FIX #3: off NOT used in SL
                # FIX #3: anchor to peak_price, not current close
                peak      = pos.get("peak_price", snap.close)
                candidate = (peak - pts) if risk.is_long else (peak + pts)
                improved  = (candidate > state.current_sl) if risk.is_long else (candidate < state.current_sl)
                if improved:
                    old_sl = state.current_sl
                    state.current_sl = candidate
                    print(f"  [BAR {bar_count[0]}] SL ratchet {old_sl:.2f} -> {candidate:.2f}")
                    trail_events.append({"bar": bar_count[0], "event": "trail_sl", "new_sl": candidate, "price": snap.close})

            print(f"[BAR {bar_count[0]}] close={snap.close:.2f} profit={profit_dist:.2f} SL={state.current_sl:.2f} stage={state.stage}", end="\r")

        if max_bars and bar_count[0] >= max_bars:
            print(f"\n\nReached {max_bars} bars - stopping")
            os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)
            if trail_events:
                pd.DataFrame(trail_events).to_csv(
                    os.path.join(os.path.dirname(__file__), "data", "trail_log.csv"), index=False
                )
                print(f"Trail log saved: phase4/data/trail_log.csv ({len(trail_events)} events)")
            await telegram.close()
            os._exit(0)

    feed = CandleFeed(on_bar)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(sys_signal.SIGINT, lambda: os._exit(0))
    await feed.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", default=0, type=int)
    args = parser.parse_args()
    asyncio.run(run(args.bars))
