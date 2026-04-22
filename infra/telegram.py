"""
infra/telegram.py — Shiva Sniper v6.5
──────────────────────────────────────────────────────────────────────
ALERTS SENT:
  Lifecycle  → Bot started / stopped / crashed
  Entry      → Signal type + fill + SL + TP + ATR + R:R
  Exit       → Entry→Exit price + P&L USD + exit reason
  Error      → Any caught exception with context label
  Daily      → Midnight IST summary: trades / win-loss / net P&L
──────────────────────────────────────────────────────────────────────
"""

import logging
from datetime import datetime, timezone, timedelta

import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger        = logging.getLogger(__name__)
IST           = timezone(timedelta(hours=5, minutes=30))
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

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """Fresh session per message — avoids stale session failures."""
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

    async def send(self, text: str) -> None:
        await self._send(text)

    # ── Helper ────────────────────────────────────────────────────────────────

    @staticmethod
    def _now_ist() -> str:
        return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    # ── Bot lifecycle ─────────────────────────────────────────────────────────

    async def notify_start(self) -> None:
        await self._send(
            f"🚀 <b>Shiva Sniper STARTED</b>\n"
            f"<code>{Telegram._now_ist()}</code>"
        )

    async def notify_stop(self) -> None:
        await self._send(
            f"🛑 <b>Shiva Sniper STOPPED</b>\n"
            f"<code>{Telegram._now_ist()}</code>"
        )

    async def notify_crash(self, reason: str) -> None:
        await self._send(
            f"💥 <b>BOT CRASHED</b>\n"
            f"<code>{Telegram._now_ist()}</code>\n\n"
            f"<b>Reason:</b>\n<code>{str(reason)[:400]}</code>"
        )

    # ── Error ─────────────────────────────────────────────────────────────────

    async def notify_error(self, context: str, error: str = "") -> None:
        body = f"⚠️ <b>ERROR — {context}</b>\n<code>{Telegram._now_ist()}</code>"
        if error:
            body += f"\n\n<code>{str(error)[:300]}</code>"
        await self._send(body)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def notify_entry(
        self,
        signal_type : str,
        entry_price : float,
        sl          : float,
        tp          : float,
        atr         : float,
        qty         : int = None,
    ) -> None:
        is_long = "Long" in signal_type
        emoji   = "🟢" if is_long else "🔴"
        side    = "LONG" if is_long else "SHORT"
        sl_dist = abs(entry_price - sl)
        tp_dist = abs(tp - entry_price)
        rr      = tp_dist / sl_dist if sl_dist > 0 else 0
        qty_str = f"  |  {qty} lot{'s' if qty != 1 else ''}" if qty else ""
        await self._send(
            f"{emoji} <b>ENTRY — {side}{qty_str}</b>\n"
            f"<code>{Telegram._now_ist()}</code>\n\n"
            f"Fill  : <b>${entry_price:,.2f}</b>\n"
            f"SL    : <code>${sl:,.2f}</code>  (-{sl_dist:.2f})\n"
            f"TP    : <code>${tp:,.2f}</code>  (+{tp_dist:.2f})\n"
            f"ATR   : <code>{atr:.2f}</code>  |  R:R <code>{rr:.2f}</code>"
        )

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def notify_exit(
        self,
        reason      : str,
        entry_price : float,
        exit_price  : float,
        real_pl     : float,
        is_long     : bool = True,
        qty         : int  = None,
    ) -> None:
        emoji   = "💰" if real_pl >= 0 else "🔻"
        pl_sign = "+" if real_pl >= 0 else ""
        side    = "LONG" if is_long else "SHORT"
        qty_str = f"  |  {qty} lot{'s' if qty != 1 else ''}" if qty else ""
        await self._send(
            f"{emoji} <b>EXIT — {side}{qty_str}</b>\n"
            f"<code>{Telegram._now_ist()}</code>\n\n"
            f"Entry  : <code>${entry_price:,.2f}</code>\n"
            f"Exit   : <b>${exit_price:,.2f}</b>\n"
            f"P&amp;L    : <b>{pl_sign}{real_pl:.4f} USD</b>\n"
            f"Reason : <code>{reason}</code>"
        )

    # ── Daily Summary ─────────────────────────────────────────────────────────

    async def notify_daily_summary(self, summary: dict) -> None:
        """summary = journal.get_daily_summary() dict."""
        date = summary.get("date", "N/A")
        if not summary or summary.get("total", 0) == 0:
            await self._send(
                f"📊 <b>Daily Summary — {date}</b>\n"
                f"<code>{Telegram._now_ist()}</code>\n\n"
                f"No trades today."
            )
            return

        pl       = summary["total_pl"]
        pl_emoji = "🟢" if pl >= 0 else "🔴"
        pl_sign  = "+" if pl >= 0 else ""
        await self._send(
            f"📊 <b>Daily Summary — {date}</b>\n"
            f"<code>{Telegram._now_ist()}</code>\n"
            f"─────────────────────\n"
            f"Trades   : <b>{summary['total']}</b>\n"
            f"✅ Wins   : <b>{summary['wins']}</b>  "
            f"❌ Losses : <b>{summary['losses']}</b>\n"
            f"Win Rate : <code>{summary['win_rate']:.1f}%</code>\n"
            f"─────────────────────\n"
            f"{pl_emoji} Net P&amp;L : <b>{pl_sign}{pl:.4f} USD</b>\n"
            f"Best      : <code>+{summary['best']:.4f} USD</code>\n"
            f"Worst     : <code>{summary['worst']:.4f} USD</code>"
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
        pass
