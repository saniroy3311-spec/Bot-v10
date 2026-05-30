"""
feed/fills_feed.py — Shiva Sniper Bot-v10  |  FIX-FILLS-WS
════════════════════════════════════════════════════════════════════════════════

Delta Exchange WebSocket fills (user data) feed.

WHY THIS EXISTS
───────────────
When mode=BRACKET, Delta's matching engine handles SL/TP at ~5ms latency.
The Python tick loop has no way to know the bracket fired until:
  A) update_bracket_sl() gets "open_order_not_found" → position check
  B) on_bar_close() state sanity check detects in_position=True but Delta flat

Path A was added in FIX-BRACKET-FIRED (manager.py) but still has a delay:
it only triggers when the trail SL TIGHTENS (i.e. pushes a new SL to Delta).
If the bracket SL fires but the trail hasn't tightened yet in that tick window,
the bot won't discover the exit until the next bracket-update attempt.

Path B only runs every 30 minutes at bar close — completely unacceptable.

THIS FILE adds Path C — a real-time WebSocket subscription to Delta's private
"fills" channel. Delta pushes a fill event within milliseconds of any order
fill, including bracket SL/TP orders. On receiving a fill for our symbol on
the close-side (sell for long, buy for short), the feed:
  1. Identifies it as a bracket-side fill (stop_loss or take_profit order type)
  2. Extracts the actual fill price
  3. Calls TrailMonitor._fire_exit() immediately with the real fill price
  4. Clears manager._bracket_active so no further update attempts are made

RESULT
──────
  Before: bracket fires → 0 to 1800 seconds to detect (bar close drift check)
  After:  bracket fires → ~50-200ms to detect (WS fill event round-trip)

Delta fills WS
──────────────
  Live:    wss://socket.india.delta.exchange
  Auth:    send {"type": "auth", "payload": {"api-key": ..., "signature": ...,
            "timestamp": ...}} after connect
  Subscribe: {"type": "subscribe", "payload": {"channels":
              [{"name": "user_trades", "symbols": ["BTCUSD"]}]}}
  Fill msg:  {"type": "user_trade", "symbol": "BTCUSD", "side": "buy/sell",
              "price": "79287.5", "size": 1,
              "order_type": "stop_loss_order" / "take_profit_order" / ...}

════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional, TYPE_CHECKING

import websockets
import websockets.exceptions

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET, SYMBOL,
)

if TYPE_CHECKING:
    from monitor.trail_loop import TrailMonitor
    from orders.manager     import OrderManager

logger = logging.getLogger("feed.fills_feed")

_WS_LIVE    = "wss://socket.india.delta.exchange"
_WS_TESTNET = "wss://testnet-socket.india.delta.exchange"

# Reconnect delay on failure (seconds)
_RECONNECT_SEC   = 5
_MAX_RECONNECT   = 60  # cap backoff

# Order types Delta uses for bracket legs
_SL_ORDER_TYPES = {"stop_loss_order", "stop_loss"}
_TP_ORDER_TYPES = {"take_profit_order", "take_profit"}
_BRACKET_ORDER_TYPES = _SL_ORDER_TYPES | _TP_ORDER_TYPES


def _make_auth_signature(ts: str) -> str:
    """HMAC-SHA256 signature for Delta WS auth."""
    msg = ("GET" + ts + "/live" + "").encode()
    return hmac.new(
        DELTA_API_SECRET.encode(), msg, hashlib.sha256
    ).hexdigest()


def _ws_symbol(ccxt_symbol: str) -> str:
    """BTC/USD:USD  →  BTCUSD"""
    return ccxt_symbol.split(":")[0].replace("/", "")


class FillsFeed:
    """
    Subscribes to Delta's user_trades WebSocket channel and fires
    TrailMonitor exits immediately when a bracket SL/TP fill is detected.

    Usage (in main.py):
        self._fills_feed = FillsFeed(
            trail_monitor = self._trail_mon,
            order_manager = self._order_mgr,
        )
        self._fills_feed.start_task()

    Call stop() on shutdown.
    """

    def __init__(
        self,
        trail_monitor: "TrailMonitor",
        order_manager: "OrderManager",
    ) -> None:
        self._trail_mon  = trail_monitor
        self._order_mgr  = order_manager
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None   # FIX: keepalive
        self._running    = False
        self._ws_symbol  = _ws_symbol(SYMBOL)
        self._ws_conn    = None                           # FIX: hold current ws ref
        self._PING_INTERVAL = 20                          # FIX: ping every 20s

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start_task(self) -> None:
        """Schedule the fills listener as a background asyncio task."""
        self._running = True
        loop = asyncio.get_running_loop()
        self._task      = loop.create_task(self._run(),         name="fills_feed")
        self._ping_task = loop.create_task(self._keepalive(),   name="fills_ping")  # FIX
        logger.info(
            f"[FILLS] FillsFeed started — listening for bracket fills on {self._ws_symbol}"
        )

    def stop(self) -> None:
        """Cancel the background task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._ping_task and not self._ping_task.done():   # FIX
            self._ping_task.cancel()                         # FIX
        self._ping_task = None                               # FIX
        logger.info("[FILLS] FillsFeed stopped.")

    # ── Keepalive ping ─────────────────────────────────────────────────────────

    async def _keepalive(self) -> None:
        """Send a ping every _PING_INTERVAL seconds to prevent silent WS drops.
        FIX-FILLS-DEAD-SOCKET: on 2 consecutive ping failures, force-close the
        socket so the reconnect loop kicks in within ~1 min instead of waiting
        ~15 min for a TCP-level timeout (ping_interval/ping_timeout are disabled
        here, so the library will not detect the dead socket on its own)."""
        fails = 0
        while self._running:
            await asyncio.sleep(self._PING_INTERVAL)
            ws = self._ws_conn
            if ws is None:
                fails = 0
                continue
            try:
                pong = await ws.ping()
                await asyncio.wait_for(asyncio.shield(pong), timeout=10)
                fails = 0
                logger.debug("[FILLS] Keepalive ping ✅")
            except Exception as e:
                fails += 1
                logger.warning(f"[FILLS] Keepalive ping failed ({fails}): {e}")
                if fails >= 2:
                    logger.warning(
                        "[FILLS] 2 pings failed — forcing reconnect (dead socket)"
                    )
                    fails = 0
                    try:
                        await ws.close()
                    except Exception:
                        pass

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Reconnect loop — maintains WS connection indefinitely."""
        attempt      = 0
        reconnect_sec = _RECONNECT_SEC

        while self._running:
            try:
                await self._connect_and_listen()
                # Clean exit (shouldn't happen unless server closed)
                attempt = 0
                reconnect_sec = _RECONNECT_SEC
            except asyncio.CancelledError:
                break
            except Exception as e:
                attempt += 1
                logger.warning(
                    f"[FILLS] WS disconnected (attempt {attempt}): {e} — "
                    f"reconnecting in {reconnect_sec}s"
                )
                await asyncio.sleep(reconnect_sec)
                reconnect_sec = min(reconnect_sec * 2, _MAX_RECONNECT)

    async def _connect_and_listen(self) -> None:
        """Single WebSocket session: auth → subscribe → process messages."""
        ws_url = _WS_TESTNET if DELTA_TESTNET else _WS_LIVE
        self._ws_conn = None  # FIX: clear before each new session

        async with websockets.connect(
            ws_url,
            ping_interval = None,   # FIX: disabled — our _keepalive() manages pings
            ping_timeout  = None,   # FIX: avoids duplicate ping conflicts
            close_timeout = 10,
        ) as ws:
            self._ws_conn = ws      # FIX: expose to keepalive task
            # ── Authenticate ──────────────────────────────────────────────────
            ts  = str(int(time.time()))
            sig = _make_auth_signature(ts)
            auth_msg = json.dumps({
                "type": "auth",
                "payload": {
                    "api-key":   DELTA_API_KEY,
                    "signature": sig,
                    "timestamp": ts,
                }
            })
            await ws.send(auth_msg)
            logger.info("[FILLS] WS auth sent — waiting for confirmation...")

            # ── Subscribe to user_trades ───────────────────────────────────────
            sub_msg = json.dumps({
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {"name": "user_trades", "symbols": [self._ws_symbol]}
                    ]
                }
            })
            await ws.send(sub_msg)
            logger.info(
                f"[FILLS] Subscribed to user_trades | symbol={self._ws_symbol}"
            )

            # ── Message loop ──────────────────────────────────────────────────
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "auth":
                    status = msg.get("status") or msg.get("result") or msg
                    logger.info(f"[FILLS] Auth response: {status}")
                    continue

                if msg_type in ("subscriptions", "heartbeat", "info"):
                    continue

                # Delta sends fill events as type="user_trade" or "fill"
                if msg_type in ("user_trade", "fill", "user_trades"):
                    await self._handle_fill(msg)

    # ── Fill handler ───────────────────────────────────────────────────────────

    async def _handle_fill(self, msg: dict) -> None:
        """
        Process a fill event from the user_trades channel.

        Only acts if:
          1. We are currently in a position (trail monitor running)
          2. The fill is for our symbol
          3. The order type is a bracket leg (stop_loss or take_profit)
          4. Exit hasn't fired yet
        """
        # Guard: only process if we have an active trail
        if not self._trail_mon._running or self._trail_mon._exit_fired:
            return

        # Symbol check
        fill_symbol = (
            msg.get("symbol") or
            (msg.get("data") or {}).get("symbol") or
            ""
        ).upper()
        if fill_symbol != self._ws_symbol:
            return

        # Extract order type
        data = msg.get("data") or msg
        order_type = (
            data.get("order_type") or
            data.get("stop_order_type") or
            ""
        ).lower().strip()

        if order_type not in _BRACKET_ORDER_TYPES:
            # Not a bracket leg — regular fill (entry), ignore
            logger.debug(f"[FILLS] Non-bracket fill ignored: order_type={order_type!r}")
            return

        # Extract fill price
        fill_price_raw = (
            data.get("fill_price") or
            data.get("price") or
            data.get("average") or
            0.0
        )
        try:
            fill_price = float(fill_price_raw)
        except (TypeError, ValueError):
            fill_price = 0.0

        # Extract fill side
        side = (data.get("side") or "").lower()

        # Determine reason label
        if order_type in _SL_ORDER_TYPES:
            reason = "Bracket SL (exchange-side)"
        else:
            reason = "Bracket TP (exchange-side)"

        logger.info(
            f"[FILLS] 🎯 Bracket fill detected via WS | "
            f"order_type={order_type} side={side} "
            f"fill_price={fill_price:.2f} reason={reason}"
        )

        # Mark bracket inactive on manager so no further update attempts
        if self._order_mgr is not None:
            self._order_mgr._bracket_active   = False
            self._order_mgr._bracket_order_id = None

        # Fire exit on trail monitor with actual exchange fill price
        if not self._trail_mon._exit_fired:
            exit_px = fill_price if fill_price > 0 else (
                self._trail_mon._state.current_sl
                if self._trail_mon._state else 0.0
            )
            asyncio.get_running_loop().create_task(
                self._trail_mon._fire_exit(exit_px, reason, source="bracket_ws")
            )
