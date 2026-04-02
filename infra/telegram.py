"""
infra/telegram.py
Telegram notifications for Shiva Sniper v12.

DELTA INDIA LOT → BTC CALCULATION in every message:
  1 lot = 0.001 BTC  (0.1 BTC = 100 lots)

  P/L = price_move × qty_btc
  Example: 1 lot, entry=66387, exit=66407 SHORT
    qty_btc = 0.001 BTC
    move    = 66387 - 66407 = -20 pts  (SHORT, price went UP = LOSS)
    raw_pl  = -20 × 0.001  = -0.02 USD
    comm    = 0.001 × (66387+66407) × 0.0005 = 0.06640 USD
    net_pl  = -0.020 - 0.066 = -0.086 USD  ← matches TV screenshot (-0.086 USD)
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from risk.calculator import lots_to_btc, calc_pl_breakdown, DELTA_INDIA_BTC_PER_LOT

logger = logging.getLogger(__name__)


class Telegram:
    BASE = "https://api.telegram.org/bot"

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def send(self, text: str) -> None:
        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            url  = f"{self.BASE}{TELEGRAM_BOT_TOKEN}/sendMessage"
            resp = await self._session.post(url, json={
                "chat_id"   : TELEGRAM_CHAT_ID,
                "text"      : text,
                "parse_mode": "HTML",
            })
            data = await resp.json()
            if not data.get("ok"):
                logger.error(f"Telegram API error: {data}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    async def notify_start(self) -> None:
        from config import ALERT_QTY
        qty_btc = lots_to_btc(ALERT_QTY)
        await self.send(
            f"🚀 <b>BOT STARTED</b> — Shiva Sniper v12\n"
            f"<code>Size: {ALERT_QTY} lots = {qty_btc:.3f} BTC</code>\n"
            f"<code>1 lot = {DELTA_INDIA_BTC_PER_LOT} BTC (Delta India)</code>"
        )

    async def notify_stop(self) -> None:
        await self.send("🛑 <b>BOT STOPPED</b> — Shiva Sniper v12 offline.")

    async def notify_error(self, error: str) -> None:
        await self.send(
            f"⚠️ <b>BOT ERROR</b>\n"
            f"<code>{error[:400]}</code>"
        )

    async def notify_entry(self, signal_type: str, entry_price: float,
                           sl: float, tp: float, atr: float,
                           qty: int = None) -> None:
        from config import ALERT_QTY
        qty      = qty or ALERT_QTY
        qty_btc  = lots_to_btc(qty)
        sl_pts   = abs(entry_price - sl)
        tp_pts   = abs(tp - entry_price)
        notional = entry_price * qty_btc
        # Max loss if SL hit (before commission)
        max_loss = sl_pts * qty_btc
        # Max gain if TP hit (before commission)
        max_gain = tp_pts * qty_btc
        direction = "LONG 🟢" if "Long" in signal_type else "SHORT 🔴"

        await self.send(
            f"{'🟢' if 'Long' in signal_type else '🔴'} <b>ENTRY — {signal_type}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry   : <code>{entry_price:,.2f} USD</code>\n"
            f"🛑 SL      : <code>{sl:,.2f}</code>  (-{sl_pts:.1f} pts)\n"
            f"🎯 TP      : <code>{tp:,.2f}</code>  (+{tp_pts:.1f} pts)\n"
            f"📊 ATR     : <code>{atr:.2f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Size    : <code>{qty} lots = {qty_btc:.4f} BTC</code>\n"
            f"💼 Notional: <code>{notional:,.3f} USD</code>\n"
            f"📉 Max Loss: <code>~{max_loss:.4f} USD</code>\n"
            f"📈 Max Gain: <code>~{max_gain:.4f} USD</code>"
        )

    async def notify_exit(self, reason: str, entry_price: float,
                          exit_price: float, real_pl: float,
                          qty: int = None, is_long: bool = True) -> None:
        from config import ALERT_QTY
        qty  = qty or ALERT_QTY
        plb  = calc_pl_breakdown(entry_price, exit_price, is_long, qty)
        emoji    = "💰" if plb["net_pl_usdt"] >= 0 else "🔻"
        pl_sign  = "+" if plb["net_pl_usdt"] >= 0 else ""
        direction = "LONG" if is_long else "SHORT"

        await self.send(
            f"{emoji} <b>EXIT — {reason}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry   : <code>{entry_price:,.2f} USD</code>\n"
            f"🏁 Exit    : <code>{exit_price:,.2f} USD</code>\n"
            f"📏 Move    : <code>{pl_sign}{plb['price_move']:,.2f} pts</code>\n"
            f"📐 Dir     : <code>{direction}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Size    : <code>{plb['lots']} lots = {plb['qty_btc']:.4f} BTC</code>\n"
            f"💵 Gross   : <code>{pl_sign}{plb['raw_pl_usdt']:.6f} USD</code>\n"
            f"🏦 Comm    : <code>-{plb['commission_usdt']:.6f} USD</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>Net P/L : {pl_sign}{plb['net_pl_usdt']:.6f} USD</b>\n"
            f"📊 Return  : <code>{pl_sign}{plb['net_pl_pct']:.4f}%</code>"
        )

    async def notify_breakeven(self, entry_price: float) -> None:
        await self.send(
            f"🛡️ <b>BREAKEVEN</b> — SL moved to entry\n"
            f"<code>{entry_price:,.2f}</code>"
        )

    async def notify_trail_stage(self, old_stage: int, new_stage: int,
                                  price: float, new_sl: float) -> None:
        await self.send(
            f"📈 <b>TRAIL STAGE {old_stage} → {new_stage}</b>\n"
            f"Price  : <code>{price:,.2f}</code>\n"
            f"New SL : <code>{new_sl:,.2f}</code>"
        )

    async def notify_max_sl(self, price: float, entry_price: float) -> None:
        await self.send(
            f"🚨 <b>MAX SL — Emergency Close</b>\n"
            f"Entry : <code>{entry_price:,.2f}</code>\n"
            f"Price : <code>{price:,.2f}</code>"
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
