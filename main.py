"""
main_wiring_snippet.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
Drop-in code for main.py showing the THREE changes needed:

  1. Build EngineState + TelegramController in __init__
  2. Spawn the controller task in run()
  3. Gate new-entry logic on `self._state.running`
  4. Convert BTC-size config → Lots via risk.lot_sizing.btc_to_lots
     before sending to OrderManager / Delta API
──────────────────────────────────────────────────────────────────────
"""

# ── 1. Imports (add at top of main.py) ───────────────────────────────────────
from infra.telegram_controller import TelegramController, EngineState
from risk.lot_sizing           import btc_to_lots
from config                    import POSITION_BTC_SIZE  # NEW config key, default 0.001


# ── 2. Inside ShivaSniper.__init__ ───────────────────────────────────────────
def __init__(self):
    # ... existing init code ...
    self._telegram = Telegram()
    self._state    = EngineState(running=True)           # NEW shared flag
    self._tg_ctrl  = TelegramController(                 # NEW controller
        engine_state = self._state,
        telegram     = self._telegram,
        journal      = self._journal,
        order_mgr    = self._order_mgr,
    )
    # ... rest of init ...


# ── 3. Inside run() — spawn the listener task ────────────────────────────────
async def run(self):
    await self._telegram.notify_start()
    asyncio.create_task(self._tg_ctrl.run())             # NEW long-poll task
    # ... existing run code ...


# ── 4. Gate entries on running flag ──────────────────────────────────────────
async def _handle_signal(self, signal):
    if not self._state.running:
        logger.info("Signal ignored — engine PAUSED via /stop_bot")
        return
    # ... existing entry logic ...


# ── 5. Compute lots from BTC size before placing the order ───────────────────
async def _place_entry(self, signal_type, entry_price, sl, tp, atr):
    qty_lots = btc_to_lots(POSITION_BTC_SIZE)            # e.g. 0.05 BTC → 50 lots
    logger.info(f"[ENTRY] {signal_type}  btc={POSITION_BTC_SIZE}  qty={qty_lots} lots")

    await self._order_mgr.place_entry(
        side=("buy" if "Long" in signal_type else "sell"),
        qty=qty_lots,
        entry_price=entry_price,
        sl=sl, tp=tp,
    )
    await self._telegram.notify_entry(
        signal_type=signal_type,
        entry_price=entry_price,
        sl=sl, tp=tp, atr=atr,
        qty=qty_lots,                                    # passes through to notification
    )
    self._journal.open_trade(
        signal_type=signal_type,
        is_long=("Long" in signal_type),
        entry_price=entry_price,
        sl=sl, tp=tp, atr=atr,
        qty=qty_lots,
    )


# ── 6. config.py addition ────────────────────────────────────────────────────
# POSITION_BTC_SIZE = float(os.environ.get("POSITION_BTC_SIZE", "0.001"))
#   0.001 BTC = 1 lot   (min)
#   0.1   BTC = 100 lots
#   1.0   BTC = 1000 lots
