"""
infra/telegram.py — Shiva Sniper v6.5
Simple entry / exit price alerts only.
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_PLACEHOLDERS = {"YOUR_BOT_TOKEN", "YOUR_CHAT_ID", "", None}


class Telegram:
    BASE = "https://api.telegram.org/bot"

    def __init__(self):
        self._enabled = (
            TELEGRAM_BOT_TOKEN not in _PLACEHOLDERS
            and TELEGRAM_CHAT_ID not in _PLACEHOLDERS
        )
        if not self._enabled:
            logger.warning(
                "Telegram disabled — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
                "in your .env to enable notifications."
            )

    async def _send(self, text: str) -> None:
        """Open a fresh session per message — avoids silent failures from a
        closed/stale session that was reused across event-loop restarts."""
        if not self._enabled:
            return
        url = f"{self.BASE}{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(url, json={
                    "chat_id"   : TELEGRAM_CHAT_ID,
                    "text"      : text,
                    "parse_mode": "HTML",
                }, timeout=aiohttp.ClientTimeout(total=10))
                data = await resp.json()
                if not data.get("ok"):
                    logger.error(f"Telegram API error: {data}")
                else:
                    logger.info(f"Telegram sent: {text!r}")
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    # Keep backward compat
    async def send(self, text: str) -> None:
        await self._send(text)

    # ── Bot lifecycle ─────────────────────────────────────────────────────────

    async def notify_start(self) -> None:
        await self._send("🚀 <b>Shiva Sniper started</b>")

    async def notify_stop(self) -> None:
        await self._send("🛑 <b>Shiva Sniper stopped</b>")

    async def notify_error(self, error: str) -> None:
        await self._send(f"⚠️ <b>Error:</b> <code>{error[:400]}</code>")

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def notify_entry(
        self,
        signal_type: str,
        entry_price: float,
        sl: float,
        tp: float,
        atr: float,
        qty: int = None,
    ) -> None:
        emoji = "🟢" if "Long" in signal_type else "🔴"
        direction = "Long" if "Long" in signal_type else "Short"
        await self._send(
            f"{emoji} <b>Bot entry {direction}: {entry_price:,.2f}</b>"
        )

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def notify_exit(
        self,
        reason: str,
        entry_price: float,
        exit_price: float,
        real_pl: float,
        is_long: bool = True,
        qty: int = None,
    ) -> None:
        emoji = "💰" if real_pl >= 0 else "🔻"
        await self._send(
            f"{emoji} <b>Exit: {exit_price:,.2f}</b>"
        )

    # ── Silenced ──────────────────────────────────────────────────────────────

    async def notify_breakeven(self, entry_price: float) -> None:
        pass

    async def notify_trail_stage(
        self, old_stage: int, new_stage: int, price: float, new_sl: float
    ) -> None:
        pass

    async def notify_max_sl(self, price: float, entry_price: float) -> None:
        pass

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        pass  # No persistent session to close
