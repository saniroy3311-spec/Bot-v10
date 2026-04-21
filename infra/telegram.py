"""
infra/telegram.py — Shiva Sniper v6.5
Entry and exit notifications only. All trail/BE/stage messages silenced.
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_QTY, COMMISSION_PCT
from risk.calculator import lots_to_btc, DELTA_INDIA_BTC_PER_LOT

logger = logging.getLogger(__name__)


class Telegram:
    BASE = "https://api.telegram.org/bot"

    # Placeholder values that mean "not configured"
    _PLACEHOLDERS = {"YOUR_BOT_TOKEN", "YOUR_CHAT_ID", "", None}

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._enabled = (
            TELEGRAM_BOT_TOKEN not in self._PLACEHOLDERS
            and TELEGRAM_CHAT_ID not in self._PLACEHOLDERS
        )
        if not self._enabled:
            logger.warning(
                "Telegram disabled — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID "
                "in your .env to enable notifications."
            )

    async def _send(self, text: str) -> None:
        if not self._enabled:
            return
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

    # Keep backward compat — old code calls send(), new calls _send()
    async def send(self, text: str) -> None:
        await self._send(text)

    # ── Bot lifecycle (keep these) ─────────────────────────────────────────────

    async def notify_start(self) -> None:
        qty_btc = lots_to_btc(ALERT_QTY)
        await self._send(
            f"🚀 <b>Shiva Sniper started</b>\n"
            f"<code>Size: {ALERT_QTY} lots = {qty_btc:.4f} BTC</code>"
        )

    async def notify_stop(self) -> None:
        await self._send("🛑 <b>Shiva Sniper stopped</b>")

    async def notify_error(self, error: str) -> None:
        await self._send(
            f"⚠️ <b>BOT ERROR</b>\n"
            f"<code>{error[:400]}</code>"
        )

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
        qty     = qty or ALERT_QTY
        qty_btc = lots_to_btc(qty)
        sl_pts  = abs(entry_price - sl)
        tp_pts  = abs(tp - entry_price)
        notional = entry_price * qty_btc
        max_loss = sl_pts * qty_btc
        max_gain = tp_pts * qty_btc
        emoji   = "🟢" if "Long" in signal_type else "🔴"

        await self._send(
            f"{emoji} <b>ENTRY — {signal_type}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry    : <code>{entry_price:,.3f} USD</code>\n"
            f"🛑 SL         : <code>{sl:,.2f}</code>  (-{sl_pts:.1f} pts)\n"
            f"🎯 TP         : <code>{tp:,.2f}</code>  (+{tp_pts:.1f} pts)\n"
            f"📊 ATR       : <code>{atr:.2f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Size      : <code>{qty} lots = {qty_btc:.4f} BTC</code>\n"
            f"💼 Notional : <code>{notional:,.3f} USD</code>\n"
            f"📉 Max Loss : <code>~{max_loss:.4f} USD</code>\n"
            f"📈 Max Gain : <code>~{max_gain:.4f} USD</code>"
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
        qty      = qty or ALERT_QTY
        qty_btc  = lots_to_btc(qty)
        move_pts = exit_price - entry_price if is_long else entry_price - exit_price
        gross_pl = move_pts * qty_btc
        # FIX-COMM-002: Exit is bracket/limit (maker = 0%). Only charge entry taker fee.
        comm     = entry_price * qty_btc * COMMISSION_PCT
        emoji    = "💰" if real_pl >= 0 else "🔻"
        direction = "LONG" if is_long else "SHORT"
        pl_sign  = "+" if real_pl >= 0 else ""

        await self._send(
            f"{emoji} <b>EXIT — {reason}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry   : <code>{entry_price:,.3f} USD</code>\n"
            f"🏁 Exit    : <code>{exit_price:,.3f} USD</code>\n"
            f"↔️ Move   : <code>{move_pts:+,.2f} pts</code>\n"
            f"📐 Dir     : <code>{direction}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔢 Size    : <code>{qty} lots = {qty_btc:.4f} BTC</code>\n"
            f"💵 Gross   : <code>{pl_sign}{gross_pl:.6f} USD</code>\n"
            f"🏦 Comm    : <code>-{comm:.6f} USD</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>Net P/L : {pl_sign}{real_pl:.6f} USD</b>\n"
            f"📊 Return  : <code>{pl_sign}{real_pl / (entry_price * qty_btc) * 100:.4f}%</code>"
        )

    # ── Silenced — no noise ───────────────────────────────────────────────────

    async def notify_breakeven(self, entry_price: float) -> None:
        pass  # silenced

    async def notify_trail_stage(
        self, old_stage: int, new_stage: int, price: float, new_sl: float
    ) -> None:
        pass  # silenced

    async def notify_max_sl(self, price: float, entry_price: float) -> None:
        pass  # silenced

    # ── Cleanup ───────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
