"""
infra/telegram.py
Telegram notifications for Shiva Sniper v6.5.

FIX: Added notify_error() — referenced in main.py _process_bar() error handler
     but missing from original file, causing AttributeError on exception paths.
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

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
        await self.send("🚀 <b>BOT STARTED</b> — Shiva Sniper v6.5 is now LIVE.")

    async def notify_stop(self) -> None:
        await self.send("🛑 <b>BOT STOPPED</b> — Shiva Sniper v6.5 is offline.")

    async def notify_error(self, error: str) -> None:
        """FIX: was missing — referenced in main.py error handler."""
        await self.send(
            f"⚠️ <b>BOT ERROR</b>\n"
            f"<code>{error[:400]}</code>"
        )

    async def notify_entry(self, signal_type: str, entry_price: float,
                           sl: float, tp: float, atr: float) -> None:
        emoji = "🟢" if "Long" in signal_type else "🔴"
        await self.send(
            f"{emoji} <b>ENTRY — {signal_type}</b>\n"
            f"Price : <code>{entry_price:.2f}</code>\n"
            f"SL    : <code>{sl:.2f}</code>\n"
            f"TP    : <code>{tp:.2f}</code>\n"
            f"ATR   : <code>{atr:.2f}</code>"
        )

    async def notify_exit(self, reason: str, entry_price: float,
                          exit_price: float, real_pl: float) -> None:
        emoji = "💰" if real_pl >= 0 else "🔻"
        await self.send(
            f"{emoji} <b>EXIT — {reason}</b>\n"
            f"Entry : <code>{entry_price:.2f}</code>\n"
            f"Exit  : <code>{exit_price:.2f}</code>\n"
            f"P/L   : <code>{real_pl:+.2f} USDT</code>"
        )

    async def notify_breakeven(self, entry_price: float) -> None:
        await self.send(
            f"🛡️ <b>BREAKEVEN HIT</b>\n"
            f"SL moved to entry: <code>{entry_price:.2f}</code>"
        )

    async def notify_trail_stage(self, old_stage: int, new_stage: int,
                                  price: float, new_sl: float) -> None:
        await self.send(
            f"📈 <b>TRAIL STAGE {old_stage} → {new_stage}</b>\n"
            f"Price  : <code>{price:.2f}</code>\n"
            f"New SL : <code>{new_sl:.2f}</code>"
        )

    async def notify_max_sl(self, price: float, entry_price: float) -> None:
        await self.send(
            f"🚨 <b>MAX SL HIT — Emergency Close</b>\n"
            f"Entry : <code>{entry_price:.2f}</code>\n"
            f"Price : <code>{price:.2f}</code>"
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
