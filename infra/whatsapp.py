"""
infra/whatsapp.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
WhatsApp notifier — identical alert set to infra/telegram.py but
delivered via the WhatsApp Business Cloud API (Meta Graph API).

ALERTS SENT:
  Lifecycle  → Bot started / stopped / crashed
  Entry      → Signal type + fill + SL + TP + ATR + R:R + qty (lots, BTC)
  Exit       → Entry→Exit price + Points Captured + P&L USD + reason
  Error      → Any caught exception with context label
  Daily      → Midnight IST summary: trades / win-loss / net P&L

SETUP (free tier — WhatsApp Business Cloud API):
  1. Create a Meta Developer account → https://developers.facebook.com
  2. Create an App → Business type → add "WhatsApp" product.
  3. In WhatsApp → Getting Started, note:
       • Phone Number ID  (WHATSAPP_PHONE_NUMBER_ID)
       • Temporary or permanent access token (WHATSAPP_ACCESS_TOKEN)
  4. Add the recipient number to the test allowlist (Sandbox) or go live.
  5. Set WHATSAPP_TO_NUMBER to the recipient's full international number,
     e.g. "919876543210"  (country code + number, no + or spaces).
  6. Add to .env:
       WHATSAPP_ACCESS_TOKEN=<token>
       WHATSAPP_PHONE_NUMBER_ID=<phone-number-id>
       WHATSAPP_TO_NUMBER=<recipient-number>

NOTE ON FORMATTING:
  WhatsApp text messages do NOT support HTML.  Bold uses *text*, italic
  uses _text_, monospace uses ```text```.  This file converts the same
  logical content to WhatsApp-safe markup so alerts look clean.

RUNS ALONGSIDE TELEGRAM:
  Both notifiers are instantiated independently in main.py.  They do not
  replace each other — every alert fires on both channels.
──────────────────────────────────────────────────────────────────────
"""

import logging
from datetime import datetime, timezone, timedelta

import aiohttp

# ── Config keys ────────────────────────────────────────────────────────────
# Add these three variables to config.py (and your .env file).
# If the keys are missing the notifier silently disables itself.
try:
    from config import (
        WHATSAPP_ACCESS_TOKEN,
        WHATSAPP_PHONE_NUMBER_ID,
        WHATSAPP_TO_NUMBER,
    )
except ImportError:
    WHATSAPP_ACCESS_TOKEN    = None
    WHATSAPP_PHONE_NUMBER_ID = None
    WHATSAPP_TO_NUMBER       = None

from risk.lot_sizing import compute_points, lots_to_btc

logger        = logging.getLogger(__name__)
IST           = timezone(timedelta(hours=5, minutes=30))
_PLACEHOLDERS = {"YOUR_ACCESS_TOKEN", "YOUR_PHONE_NUMBER_ID", "YOUR_TO_NUMBER", "", None}

_GRAPH_URL = "https://graph.facebook.com/v20.0/{phone_number_id}/messages"


class WhatsApp:
    """
    Async WhatsApp Business Cloud API notifier.

    Drop-in companion to infra/telegram.py — exposes the same public
    async methods (notify_start, notify_stop, notify_entry, …) so
    main.py can call both with identical code.
    """

    def __init__(self):
        self._enabled = (
            WHATSAPP_ACCESS_TOKEN    not in _PLACEHOLDERS
            and WHATSAPP_PHONE_NUMBER_ID not in _PLACEHOLDERS
            and WHATSAPP_TO_NUMBER       not in _PLACEHOLDERS
        )
        if not self._enabled:
            logger.warning(
                "WhatsApp disabled — set WHATSAPP_ACCESS_TOKEN, "
                "WHATSAPP_PHONE_NUMBER_ID, and WHATSAPP_TO_NUMBER in .env "
                "to enable notifications."
            )
        else:
            self._url = _GRAPH_URL.format(phone_number_id=WHATSAPP_PHONE_NUMBER_ID)
            self._headers = {
                "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                "Content-Type":  "application/json",
            }

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _send(self, text: str) -> None:
        """Send a plain-text WhatsApp message (fresh session per call)."""
        if not self._enabled:
            return
        payload = {
            "messaging_product": "whatsapp",
            "to":                WHATSAPP_TO_NUMBER,
            "type":              "text",
            "text":              {"preview_url": False, "body": text},
        }
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    self._url,
                    json=payload,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                # Always log the full API response so delivery failures are visible
                if resp.status != 200 or "messages" not in data:
                    logger.error(f"WhatsApp API error {resp.status}: {data}")
                else:
                    msg_id = data.get("messages", [{}])[0].get("id", "no-id")
                    logger.info(f"WhatsApp sent OK | msg_id={msg_id} | body={text!r}")
        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")

    async def send(self, text: str) -> None:
        """Public send — converts basic HTML tags to WhatsApp markup then sends."""
        await self._send(_html_to_wa(text))

    # ── Helper ────────────────────────────────────────────────────────────────

    @staticmethod
    def _now_ist() -> str:
        return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    # ── Bot lifecycle ─────────────────────────────────────────────────────────

    async def notify_start(self) -> None:
        await self._send(
            f"🚀 *Shiva Sniper STARTED*\n"
            f"`{WhatsApp._now_ist()}`"
        )

    async def notify_stop(self) -> None:
        await self._send(
            f"🛑 *Shiva Sniper STOPPED*\n"
            f"`{WhatsApp._now_ist()}`"
        )

    async def notify_crash(self, reason: str) -> None:
        await self._send(
            f"💥 *BOT CRASHED*\n"
            f"`{WhatsApp._now_ist()}`\n\n"
            f"*Reason:*\n```{str(reason)[:400]}```"
        )

    # ── Error ─────────────────────────────────────────────────────────────────

    async def notify_error(self, context: str, error: str = "") -> None:
        body = f"⚠️ *ERROR — {context}*\n`{WhatsApp._now_ist()}`"
        if error:
            body += f"\n\n```{str(error)[:300]}```"
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
        qty_str = ""
        if qty:
            qty_str = (
                f"  |  `{qty}` lot{'s' if qty != 1 else ''}"
                f"  ({lots_to_btc(qty):.4f} BTC)"
            )
        await self._send(
            f"{emoji} *ENTRY — {side}*{qty_str}\n"
            f"`{WhatsApp._now_ist()}`\n\n"
            f"Fill  : *${entry_price:,.2f}*\n"
            f"SL    : `${sl:,.2f}`  (-{sl_dist:.2f})\n"
            f"TP    : `${tp:,.2f}`  (+{tp_dist:.2f})\n"
            f"ATR   : `{atr:.2f}`  |  R:R `{rr:.2f}`"
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
        side     = "LONG" if is_long else "SHORT"
        points   = compute_points(entry_price, exit_price, is_long)
        gross    = points * (qty or 1) * 0.001
        emoji    = "💰" if gross  >= 0 else "🔻"
        pts_sign = "+" if points >= 0 else ""
        grs_sign = "+" if gross  >= 0 else ""
        qty_str  = f"  |  `{qty}` lot{'s' if qty != 1 else ''}" if qty else ""

        await self._send(
            f"{emoji} *EXIT — {side}*{qty_str}\n"
            f"`{WhatsApp._now_ist()}`\n\n"
            f"Entry         : `${entry_price:,.2f}`\n"
            f"Exit          : *${exit_price:,.2f}*\n"
            f"Points        : `{pts_sign}{points:.2f}`\n"
            f"*Gross P&L : {grs_sign}${gross:.4f} USD*\n"
            f"Reason        : `{reason}`"
        )

    # ── Daily Summary ─────────────────────────────────────────────────────────

    async def notify_daily_summary(self, summary: dict) -> None:
        date = summary.get("date", "N/A")
        if not summary or summary.get("total", 0) == 0:
            await self._send(
                f"📊 *Daily Summary — {date}*\n"
                f"`{WhatsApp._now_ist()}`\n\n"
                f"No trades today."
            )
            return

        pl       = summary["total_pl"]
        pl_emoji = "🟢" if pl >= 0 else "🔴"
        pl_sign  = "+" if pl >= 0 else ""
        await self._send(
            f"📊 *Daily Summary — {date}*\n"
            f"`{WhatsApp._now_ist()}`\n"
            f"─────────────────────\n"
            f"Trades   : *{summary['total']}*\n"
            f"✅ Wins   : *{summary['wins']}*  "
            f"❌ Losses : *{summary['losses']}*\n"
            f"Win Rate : `{summary['win_rate']:.1f}%`\n"
            f"─────────────────────\n"
            f"{pl_emoji} Gross P&L : *{pl_sign}{pl:.4f} USD*\n"
            f"Best      : `+{summary['best']:.4f} USD`\n"
            f"Worst     : `{summary['worst']:.4f} USD`"
        )

    # ── Silenced (parity with telegram.py) ───────────────────────────────────

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


# ── Utility: lightweight HTML → WhatsApp markup converter ────────────────────

def _html_to_wa(text: str) -> str:
    """
    Convert the subset of HTML tags used in telegram.py to WhatsApp-safe markup.
    Handles: <b>, <code>, <i>, <pre>, &amp;  (the only tags the bot produces).
    """
    import re
    text = text.replace("&amp;", "&")
    text = re.sub(r"<b>(.*?)</b>",       r"*\1*",   text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>",       r"_\1_",   text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`",   text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>",   r"```\1```", text, flags=re.DOTALL)
    # Strip any remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    return text
