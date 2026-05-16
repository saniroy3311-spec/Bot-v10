"""
infra/telegram_controller.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
Telegram Command & Control (long-poll, no webhook).

COMMANDS:
    /start_bot   → activates execution engine (resumes new entries)
    /stop_bot    → pauses execution (no NEW entries; open trade keeps
                   trailing & exits normally — set MANAGE_OPEN_ON_STOP
                   = "close" to flatten immediately instead)
    /status      → bot state (LIVE/PAUSED) + open position + daily P/L

Designed to coexist with the existing `infra/telegram.py` notifier
(which keeps sending entry/exit/daily alerts). This controller only
handles INBOUND commands from authorised chat IDs.
──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable

import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# Open-trade behaviour when /stop_bot is issued:
#   "hold"  → let open trade trail to its SL/TP naturally  (default, safest)
#   "close" → flatten the open position at market immediately
MANAGE_OPEN_ON_STOP = "hold"

_API = "https://api.telegram.org/bot"


class TelegramController:
    """
    Long-polls Telegram and dispatches commands into the engine.

    Wire up in main.py:
        self._tg_ctrl = TelegramController(
            engine_state = self._state,        # shared state object
            telegram     = self._telegram,     # existing notifier
            journal      = self._journal,
            order_mgr    = self._order_mgr,
        )
        asyncio.create_task(self._tg_ctrl.run())
    """

    def __init__(self, engine_state, telegram, journal, order_mgr=None):
        self._state    = engine_state          # must expose .running (bool)
        self._tg       = telegram              # for replies
        self._journal  = journal
        self._order_mgr = order_mgr            # only needed if MANAGE_OPEN_ON_STOP == "close"
        self._offset   = 0
        self._stop     = False
        self._authorised_chat_ids = {str(TELEGRAM_CHAT_ID)}

        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {
            "/start_bot": self._cmd_start,
            "/stop_bot" : self._cmd_stop,
            "/status"   : self._cmd_status,
        }

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        logger.info("TelegramController started (long-poll)")
        while not self._stop:
            try:
                updates = await self._get_updates()
                for upd in updates:
                    await self._dispatch(upd)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"TelegramController loop error: {e}")
                await asyncio.sleep(3)

    def stop(self) -> None:
        self._stop = True

    # ── Long-poll ────────────────────────────────────────────────────────────

    async def _get_updates(self) -> list[dict]:
        url = f"{_API}{TELEGRAM_BOT_TOKEN}/getUpdates"
        params = {"timeout": 25, "offset": self._offset, "allowed_updates": ["message"]}
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params,
                             timeout=aiohttp.ClientTimeout(total=35)) as r:
                data = await r.json()
        if not data.get("ok"):
            logger.error(f"getUpdates failed: {data}")
            return []
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def _dispatch(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()

        if chat_id not in self._authorised_chat_ids:
            logger.warning(f"Ignoring command from unauthorised chat_id={chat_id}")
            return

        # Strip @BotName suffix if user typed /status@MyBot
        cmd = text.split("@", 1)[0].split()[0] if text else ""
        handler = self._handlers.get(cmd)
        if not handler:
            return  # silently ignore non-commands
        try:
            await handler(msg)
        except Exception as e:
            logger.exception(f"Handler {cmd} failed: {e}")
            await self._tg.send(f"⚠️ <code>{cmd}</code> failed: <code>{e}</code>")

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _cmd_start(self, msg: dict) -> None:
        if self._state.running:
            await self._tg.send("ℹ️ Bot already <b>LIVE</b>.")
            return
        self._state.running = True
        self._journal.log_event("bot_resumed", "via /start_bot")
        logger.info("Engine resumed via Telegram /start_bot")
        await self._tg.send("🟢 <b>Bot RESUMED</b> — accepting new entries.")

    async def _cmd_stop(self, msg: dict) -> None:
        if not self._state.running:
            await self._tg.send("ℹ️ Bot already <b>PAUSED</b>.")
            return
        self._state.running = False
        self._journal.log_event("bot_paused", "via /stop_bot")
        logger.info("Engine paused via Telegram /stop_bot")

        note = ""
        if MANAGE_OPEN_ON_STOP == "close" and self._order_mgr is not None:
            open_t = self._journal.get_open_trade()
            if open_t:
                try:
                    await self._order_mgr.close_position_market(reason="telegram_stop")
                    note = "\nOpen position flattened at market."
                except Exception as e:
                    note = f"\n⚠️ Failed to flatten: <code>{e}</code>"
        else:
            open_t = self._journal.get_open_trade()
            if open_t:
                note = "\nOpen position retained — will exit on SL/TP/trail."

        await self._tg.send(
            f"🟡 <b>Bot PAUSED</b> — no new entries.{note}"
        )

    async def _cmd_status(self, msg: dict) -> None:
        state = "🟢 LIVE" if self._state.running else "🟡 PAUSED"
        open_t = self._journal.get_open_trade()
        summary = self._journal.get_daily_summary() or {}

        if open_t:
            side = "LONG" if open_t["is_long"] else "SHORT"
            pos_block = (
                f"<b>Open Position</b>\n"
                f"  {side}  |  Qty: <code>{open_t['qty']}</code> lots\n"
                f"  Entry : <code>${open_t['entry_price']:,.2f}</code>\n"
                f"  SL    : <code>${open_t['current_sl']:,.2f}</code>\n"
                f"  TP    : <code>${open_t['tp']:,.2f}</code>\n"
                f"  Stage : <code>{open_t['trail_stage']}</code>"
            )
        else:
            pos_block = "<b>Open Position</b>\n  <i>None</i>"

        pl       = summary.get("total_pl", 0.0)
        pl_sign  = "+" if pl >= 0 else ""
        pl_emoji = "🟢" if pl >= 0 else "🔴"

        await self._tg.send(
            f"📋 <b>Shiva Sniper STATUS</b>\n"
            f"State : <b>{state}</b>\n\n"
            f"{pos_block}\n\n"
            f"<b>Today ({summary.get('date','—')})</b>\n"
            f"  Trades   : <code>{summary.get('total', 0)}</code>  "
            f"(W:{summary.get('wins',0)} / L:{summary.get('losses',0)})\n"
            f"  Win Rate : <code>{summary.get('win_rate', 0):.1f}%</code>\n"
            f"  {pl_emoji} P/L : <b>{pl_sign}{pl:.4f} USD</b>"
        )


# ── Shared engine state object ───────────────────────────────────────────────
class EngineState:
    """Tiny shared flag holder. Pass the SAME instance to engine + controller."""
    def __init__(self, running: bool = True):
        self.running = running
