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

TEMPLATE SETUP (bypasses 24-hour session window — REQUIRED for reliability):
  Create a template named "bot_alert" in Meta Business Manager:
    • Category : Utility
    • Language : English (en)
    • Body     : {{1}}          ← single variable, the full alert text
  Once approved, add to .env:
       WHATSAPP_TEMPLATE_NAME=bot_alert   (or your chosen name)
  The bot will always use the template for proactive alerts so messages
  are delivered even when the recipient has not written in the last 24h.

NOTE ON FORMATTING:
  WhatsApp text messages do NOT support HTML.  Bold uses *text*, italic
  uses _text_, monospace uses ```text```.  This file converts the same
  logical content to WhatsApp-safe markup so alerts look clean.
  Template body text strips markdown symbols because Meta rejects them
  inside template variables on some accounts — plain text is used there.

RUNS ALONGSIDE TELEGRAM:
  Both notifiers are instantiated independently in main.py.  They do not
  replace each other — every alert fires on both channels.
──────────────────────────────────────────────────────────────────────
"""

import logging
import re
from datetime import datetime, timezone, timedelta

import aiohttp

# ── Config keys ────────────────────────────────────────────────────────────
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

# Optional: template name in .env / config.py
# If not set, falls back to plain-text only (subject to 24-h window).
try:
    from config import WHATSAPP_TEMPLATE_NAME          # e.g. "bot_alert"
except ImportError:
    WHATSAPP_TEMPLATE_NAME = None

try:
    from config import WHATSAPP_TEMPLATE_LANG          # e.g. "en" or "en_US"
except ImportError:
    WHATSAPP_TEMPLATE_LANG = "en"

from risk.lot_sizing import compute_points, lots_to_btc

logger        = logging.getLogger(__name__)
IST           = timezone(timedelta(hours=5, minutes=30))
_PLACEHOLDERS = {"YOUR_ACCESS_TOKEN", "YOUR_PHONE_NUMBER_ID", "YOUR_TO_NUMBER", "", None}

_GRAPH_URL = "https://graph.facebook.com/v20.0/{phone_number_id}/messages"


class WhatsApp:
    """
    Async WhatsApp Business Cloud API notifier.

    Sending strategy (per call):
      1. If WHATSAPP_TEMPLATE_NAME is configured → send via approved template.
         Template messages bypass the 24-hour session window, so alerts
         arrive even when the recipient hasn't messaged the bot recently.
      2. If no template is configured → fall back to plain free-form text
         (works only within 24 h of the last incoming user message).

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
        self._use_template = (
            self._enabled
            and WHATSAPP_TEMPLATE_NAME not in _PLACEHOLDERS
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
            if self._use_template:
                logger.info(
                    f"WhatsApp template mode ON — using template "
                    f"'{WHATSAPP_TEMPLATE_NAME}' (lang={WHATSAPP_TEMPLATE_LANG}). "
                    f"24-hour session window bypassed."
                )
            else:
                logger.warning(
                    "WhatsApp template NOT configured — alerts subject to "
                    "24-hour session window. Add WHATSAPP_TEMPLATE_NAME to "
                    ".env to fix missed messages."
                )

    # ── Transport ─────────────────────────────────────────────────────────────

    async def _send_template(self, text: str) -> None:
        """
        Send via an approved Meta message template.
        The entire alert text is passed as parameter {{1}} of the template.
        Template messages bypass the 24-hour user-session window.
        """
        # Strip WhatsApp markdown (*bold*, `mono`) — some Meta accounts
        # reject markdown inside template variables. Plain text is fine.
        plain = _strip_wa_markdown(text)

        payload = {
            "messaging_product": "whatsapp",
            "to":                WHATSAPP_TO_NUMBER,
            "type":              "template",
            "template": {
                "name":     WHATSAPP_TEMPLATE_NAME,
                "language": {"code": WHATSAPP_TEMPLATE_LANG},
                "components": [
                    {
                        "type":       "body",
                        "parameters": [
                            {"type": "text", "text": plain[:1024]},
                        ],
                    }
                ],
            },
        }
        await self.__post(payload, text)

    async def _send_freeform(self, text: str) -> None:
        """
        Send a plain free-form text message.
        Only delivered if recipient messaged within the last 24 hours.
        """
        payload = {
            "messaging_product": "whatsapp",
            "to":                WHATSAPP_TO_NUMBER,
            "type":              "text",
            "text":              {"preview_url": False, "body": text},
        }
        await self.__post(payload, text)

    async def __post(self, payload: dict, log_body: str) -> None:
        """Shared HTTP POST + logging for both send paths."""
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    self._url,
                    json=payload,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                data = await resp.json()
                if resp.status != 200 or "messages" not in data:
                    logger.error(f"WhatsApp API error {resp.status}: {data}")
                else:
                    msg_id = data.get("messages", [{}])[0].get("id", "no-id")
                    logger.info(
                        f"WhatsApp sent OK | msg_id={msg_id} | body={log_body!r}"
                    )
        except Exception as e:
            logger.error(f"WhatsApp send failed: {e}")

    async def _send(self, text: str) -> None:
        """
        Main internal send: uses template if configured, else free-form.
        All notify_* methods call this.
        """
        if not self._enabled:
            return
        if self._use_template:
            await self._send_template(text)
        else:
            await self._send_freeform(text)

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


# ── Utilities ────────────────────────────────────────────────────────────────

def _html_to_wa(text: str) -> str:
    """
    Convert the subset of HTML tags used in telegram.py to WhatsApp-safe markup.
    Handles: <b>, <code>, <i>, <pre>, &amp;  (the only tags the bot produces).
    """
    text = text.replace("&amp;", "&")
    text = re.sub(r"<b>(.*?)</b>",       r"*\1*",   text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>",       r"_\1_",   text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`",   text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>",   r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _strip_wa_markdown(text: str) -> str:
    """
    Remove WhatsApp markdown symbols (*bold*, `mono`, _italic_, ```block```)
    so the plain text is safe to pass as a Meta template variable.
    Emojis and newlines are preserved — Meta handles those fine.
    """
    text = re.sub(r"```(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`",   r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_",   r"\1", text)
    return text
