"""
infra/whatsapp_controller.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
WhatsApp Command & Control — receives inbound messages via the Meta
Webhook and dispatches the same commands as TelegramController.

COMMANDS (send these as WhatsApp messages to your bot number):
    /start_bot   → activates execution engine (resumes new entries)
    /stop_bot    → pauses execution (no NEW entries; open trade keeps
                   trailing & exits normally)
    /status      → bot state (LIVE/PAUSED) + open position + daily P/L

HOW IT WORKS:
  • Meta Webhooks push inbound messages to YOUR server as HTTP POST.
  • This module starts a tiny aiohttp web server on WHATSAPP_WEBHOOK_PORT
    (default 8080) that handles the webhook handshake and incoming
    message events.
  • You must expose that port to the internet (ngrok for local dev,
    or your VPS public IP for production) and register the URL in the
    Meta Developer Console under WhatsApp → Configuration → Webhook.

SETUP STEPS:
  1. In Meta Developer Console → WhatsApp → Configuration:
       Callback URL : https://<your-server>:<port>/webhook
       Verify Token : choose any secret string → set WHATSAPP_VERIFY_TOKEN in .env
  2. Subscribe to the "messages" webhook field.
  3. Add to .env:
       WHATSAPP_VERIFY_TOKEN=<your-verify-token>
       WHATSAPP_WEBHOOK_PORT=8080          # optional, default 8080
       WHATSAPP_TO_NUMBER=<your-number>    # used for authorisation check

RUNS ALONGSIDE TELEGRAM CONTROLLER:
  Both controllers are instantiated independently in main.py.
  Commands sent on either channel take effect on the shared EngineState.
──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
import asyncio
import logging
import os
from typing import Callable, Awaitable

from aiohttp import web

# ── Config keys ────────────────────────────────────────────────────────────
try:
    from config import (
        WHATSAPP_VERIFY_TOKEN,
        WHATSAPP_TO_NUMBER,
    )
except ImportError:
    WHATSAPP_VERIFY_TOKEN = None
    WHATSAPP_TO_NUMBER    = None

WHATSAPP_WEBHOOK_PORT = int(os.environ.get("WHATSAPP_WEBHOOK_PORT", "8080"))

logger = logging.getLogger(__name__)

MANAGE_OPEN_ON_STOP = "hold"   # "hold" or "close" — same semantics as TelegramController


class WhatsAppController:
    """
    Receives inbound WhatsApp messages via Meta Webhook and dispatches
    /start_bot, /stop_bot, /status commands into the engine.

    Wire up in main.py alongside TelegramController:

        self._wa_ctrl = WhatsAppController(
            engine_state = self._state,
            whatsapp     = self._whatsapp,   # WhatsApp notifier instance
            journal      = self._journal,
            order_mgr    = self._order_mgr,
        )
        asyncio.create_task(self._wa_ctrl.run())
    """

    def __init__(self, engine_state, whatsapp, journal, order_mgr=None):
        self._state     = engine_state
        self._wa        = whatsapp
        self._journal   = journal
        self._order_mgr = order_mgr
        self._stop      = False

        # Only allow messages from the configured sender number
        self._authorised_numbers = {str(WHATSAPP_TO_NUMBER)} if WHATSAPP_TO_NUMBER else set()

        self._handlers: dict[str, Callable[[str], Awaitable[None]]] = {
            "/start_bot": self._cmd_start,
            "/stop_bot" : self._cmd_stop,
            "/status"   : self._cmd_status,
        }

        self._app    = web.Application()
        self._runner = None
        self._app.router.add_get( "/webhook", self._handle_verify)
        self._app.router.add_post("/webhook", self._handle_event)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the webhook HTTP server.  Call once via asyncio.create_task."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", WHATSAPP_WEBHOOK_PORT)
        await site.start()
        logger.info(
            f"WhatsAppController webhook server started on port {WHATSAPP_WEBHOOK_PORT}"
        )
        while not self._stop:
            await asyncio.sleep(1)

    def stop(self) -> None:
        self._stop = True

    async def cleanup(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ── Webhook verification (GET) ────────────────────────────────────────────

    async def _handle_verify(self, request: web.Request) -> web.Response:
        """
        Meta sends a GET to verify the webhook endpoint.
        Responds with hub.challenge when the verify token matches.
        """
        mode      = request.rel_url.query.get("hub.mode")
        token     = request.rel_url.query.get("hub.verify_token")
        challenge = request.rel_url.query.get("hub.challenge")

        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            logger.info("WhatsApp webhook verified successfully.")
            return web.Response(text=challenge)
        logger.warning(f"WhatsApp webhook verification failed: mode={mode} token={token}")
        return web.Response(status=403, text="Forbidden")

    # ── Webhook event (POST) ──────────────────────────────────────────────────

    async def _handle_event(self, request: web.Request) -> web.Response:
        """Receive and dispatch inbound message events from Meta."""
        try:
            body = await request.json()
        except Exception as e:
            logger.error(f"WhatsApp webhook bad JSON: {e}")
            return web.Response(status=400)

        # Always return 200 quickly so Meta doesn't retry
        asyncio.create_task(self._process_event(body))
        return web.Response(status=200)

    async def _process_event(self, body: dict) -> None:
        try:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    for msg in value.get("messages", []):
                        await self._dispatch(msg)
        except Exception as e:
            logger.exception(f"WhatsAppController process_event error: {e}")

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        from_number = str(msg.get("from", ""))
        msg_type    = msg.get("type", "")

        if from_number not in self._authorised_numbers:
            logger.warning(f"Ignoring WhatsApp message from unauthorised number: {from_number}")
            return

        if msg_type != "text":
            return  # ignore media, reactions, etc.

        text = (msg.get("text", {}).get("body", "") or "").strip()
        cmd  = text.split()[0] if text else ""

        handler = self._handlers.get(cmd)
        if not handler:
            return  # silently ignore non-commands

        try:
            await handler(from_number)
        except Exception as e:
            logger.exception(f"WhatsApp handler {cmd} failed: {e}")
            await self._wa.send(f"⚠️ `{cmd}` failed: `{e}`")

    # ── Commands ─────────────────────────────────────────────────────────────

    async def _cmd_start(self, from_number: str) -> None:
        if self._state.running:
            await self._wa.send("ℹ️ Bot already *LIVE*.")
            return
        self._state.running = True
        self._journal.log_event("bot_resumed", "via WhatsApp /start_bot")
        logger.info("Engine resumed via WhatsApp /start_bot")
        await self._wa.send("🟢 *Bot RESUMED* — accepting new entries.")

    async def _cmd_stop(self, from_number: str) -> None:
        if not self._state.running:
            await self._wa.send("ℹ️ Bot already *PAUSED*.")
            return
        self._state.running = False
        self._journal.log_event("bot_paused", "via WhatsApp /stop_bot")
        logger.info("Engine paused via WhatsApp /stop_bot")

        note = ""
        if MANAGE_OPEN_ON_STOP == "close" and self._order_mgr is not None:
            open_t = self._journal.get_open_trade()
            if open_t:
                try:
                    await self._order_mgr.close_position_market(reason="whatsapp_stop")
                    note = "\nOpen position flattened at market."
                except Exception as e:
                    note = f"\n⚠️ Failed to flatten: `{e}`"
        else:
            open_t = self._journal.get_open_trade()
            if open_t:
                note = "\nOpen position retained — will exit on SL/TP/trail."

        await self._wa.send(f"🟡 *Bot PAUSED* — no new entries.{note}")

    async def _cmd_status(self, from_number: str) -> None:
        state   = "🟢 LIVE" if self._state.running else "🟡 PAUSED"
        open_t  = self._journal.get_open_trade()
        summary = self._journal.get_daily_summary() or {}

        if open_t:
            side = "LONG" if open_t["is_long"] else "SHORT"
            pos_block = (
                f"*Open Position*\n"
                f"  {side}  |  Qty: `{open_t['qty']}` lots\n"
                f"  Entry : `${open_t['entry_price']:,.2f}`\n"
                f"  SL    : `${open_t['current_sl']:,.2f}`\n"
                f"  TP    : `${open_t['tp']:,.2f}`\n"
                f"  Stage : `{open_t['trail_stage']}`"
            )
        else:
            pos_block = "*Open Position*\n  _None_"

        pl       = summary.get("total_pl", 0.0)
        pl_sign  = "+" if pl >= 0 else ""
        pl_emoji = "🟢" if pl >= 0 else "🔴"

        await self._wa.send(
            f"📋 *Shiva Sniper STATUS*\n"
            f"State : *{state}*\n\n"
            f"{pos_block}\n\n"
            f"*Today ({summary.get('date', '—')})*\n"
            f"  Trades   : `{summary.get('total', 0)}`  "
            f"(W:{summary.get('wins', 0)} / L:{summary.get('losses', 0)})\n"
            f"  Win Rate : `{summary.get('win_rate', 0):.1f}%`\n"
            f"  {pl_emoji} P/L : *{pl_sign}{pl:.4f} USD*"
        )
