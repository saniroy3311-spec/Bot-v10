"""
orders/manager.py  —  Shiva Sniper v10  (BUG-FIX-AUDIT-v1)
═══════════════════════════════════════════════════════════════════════
FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
FIX-OM-001 | CATASTROPHIC — Added fetch_ticker() method.
  trail_loop._get_mark_price() calls self._order_mgr.fetch_ticker().
  OrderManager had no such method → AttributeError silently swallowed
  → _get_mark_price() returned None on every call → price is None or
  price <= 0 → _evaluate_tick() never called → tick loop is a no-op.
  This is the ROOT CAUSE of all intrabar exit failures.

FIX-OM-002 | Mark price extraction uses Delta India-specific field path:
  ticker["info"]["mark_price"] is the raw exchange field on Delta India.
  The standard ccxt "markPrice" key may or may not be populated depending
  on the ccxt version. We now try both in priority order.
"""

import asyncio
import logging
from typing import Optional

import socket
import aiohttp
import ccxt.async_support as ccxt

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY,
)

logger = logging.getLogger(__name__)

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"


def build_exchange() -> ccxt.delta:
    base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
    params = {
        "apiKey"         : DELTA_API_KEY,
        "secret"         : DELTA_API_SECRET,
        "enableRateLimit": True,
        "urls": {"api": {"public": base_url, "private": base_url}},
    }
    exchange = ccxt.delta(params)

    # Force IPv4 — VPS is dual-stack, aiohttp resolves IPv6 by default.
    # Delta API keys are IPv4-whitelisted only; IPv6 gets rejected.
    connector = aiohttp.TCPConnector(family=socket.AF_INET, force_close=True)
    exchange.session = aiohttp.ClientSession(connector=connector)

    logger.info(
        f"Exchange built → {'TESTNET' if DELTA_TESTNET else 'LIVE'} | "
        f"endpoint={base_url} | qty={ALERT_QTY} lots | mode=NO-BRACKET"
    )
    return exchange


async def _retry(coro_fn, retries: int = 3, delay: float = 1.0):
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            if attempt == retries:
                raise
            wait = delay * (2 ** (attempt - 1))
            logger.warning(f"Attempt {attempt} failed ({e}), retry in {wait}s")
            await asyncio.sleep(wait)


class OrderManager:
    def __init__(self):
        self.exchange = build_exchange()
        self.position: Optional[dict] = None

    async def initialize(self) -> None:
        logger.info("Loading market map from Delta India (async)...")
        await self.exchange.load_markets()
        if SYMBOL not in self.exchange.markets:
            available = [
                s for s in self.exchange.markets
                if "BTC" in s and "USD" in s and ":" in s and len(s) < 15
            ]
            raise ValueError(
                f"SYMBOL '{SYMBOL}' not found on Delta India.\n"
                f"Available BTC perpetuals: {available}\n"
                f"Fix: update SYMBOL= in your .env"
            )
        logger.info(f"Market map loaded — symbol {SYMBOL} verified ✅")

    # ── FIX-OM-001: fetch_ticker — required by trail_loop._get_mark_price() ──────

    async def fetch_ticker(self) -> Optional[dict]:
        """
        Fetch current ticker for SYMBOL.

        FIX-OM-001: This method was missing entirely from OrderManager.
        trail_loop._get_mark_price() calls self._order_mgr.fetch_ticker()
        and silently swallowed the AttributeError, returning None every tick.
        As a result _evaluate_tick() was NEVER called — the entire intrabar
        exit engine (Trail SL, TP, Max SL) was dead in production.

        Returns the raw ccxt ticker dict on success, or None on failure.
        trail_loop._get_mark_price() handles the key extraction.
        """
        try:
            ticker = await _retry(lambda: self.exchange.fetch_ticker(SYMBOL))
            return ticker
        except Exception as e:
            logger.debug(f"fetch_ticker failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────────

    async def place_entry(self, is_long: bool, sl: float, tp: float) -> dict:
        side = "buy" if is_long else "sell"
        logger.info(
            f"Placing {side.upper()} entry (no-bracket) | "
            f"SL={sl:.2f} TP={tp:.2f} managed in Python | Qty={ALERT_QTY} lots"
        )
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
        ))
        fill_price = float(order.get("average") or order.get("price") or 0)
        self.position = {
            "entry_order_id": order["id"],
            "is_long"       : is_long,
            "entry_price"   : fill_price,
        }
        logger.info(
            f"Entry filled | price={fill_price:.2f} "
            f"| SL={sl:.2f} TP={tp:.2f} tracked in trail_loop ✅"
        )
        return order

    async def modify_sl(self, new_sl: float,
                        sl_limit_buf: Optional[float] = None) -> None:
        logger.debug(f"modify_sl({new_sl:.2f}) — no-bracket mode, skipped")

    async def close_at_trail_sl(self, reason: str = "Trail SL") -> dict:
        if not self.position:
            logger.warning("close_at_trail_sl called but no position tracked")
            return {}
        is_long = self.position["is_long"]
        side    = "sell" if is_long else "buy"
        logger.info(f"Market exit ({reason}) | side={side} qty={ALERT_QTY} lots")
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
            params = {"reduce_only": True}
        ))
        self.position = None
        return order

    async def close_position(
        self,
        is_long: Optional[bool] = None,
        reason: str = "Max SL Hit",
    ) -> dict:
        """
        Close any open position with a reduce-only market order.
        `is_long` is the authoritative direction from trail_loop (frozen at entry).

        FIX-OM-003 | CRITICAL — handle `no_position_for_reduce_only` gracefully.
          When Delta's exchange-side bracket SL/TP already closed the position,
          the bot's trail_loop also tries to close it and gets this error.
          Old behaviour: exception propagates → _exit_fired resets to False →
          bot retries close every 0.1s forever in an error loop.
          Fix: catch this specific error, log a warning, treat as success
          (position is already closed — the outcome we wanted). This prevents
          the infinite retry loop and allows _on_trail_exit to fire correctly.
        """
        if is_long is None:
            if not self.position:
                logger.warning(f"close_position({reason}) called but no position tracked")
                return {}
            is_long = self.position["is_long"]

        side = "sell" if is_long else "buy"
        logger.info(
            f"Market close ({reason}) | side={side} qty={ALERT_QTY} lots is_long={is_long}"
        )
        try:
            order = await _retry(lambda: self.exchange.create_order(
                symbol = SYMBOL,
                type   = "market",
                side   = side,
                amount = ALERT_QTY,
                params = {"reduce_only": True},
            ))
            self.position = None
            return order
        except ccxt.ExchangeError as e:
            # FIX-OM-003: position already closed by exchange-side bracket order.
            # Treat as success — the position is gone, which is what we wanted.
            if "no_position_for_reduce_only" in str(e):
                logger.warning(
                    f"[OM] close_position({reason}): position already closed on exchange "
                    f"(bracket SL/TP fired). Treating as success. [FIX-OM-003]"
                )
                self.position = None
                return {"info": "already_closed"}
            raise

    async def cancel_all_orders(self) -> None:
        """Cancel every open order on SYMBOL. No-op in NO-BRACKET mode normally."""
        try:
            orders = await _retry(lambda: self.exchange.fetch_open_orders(SYMBOL))
        except Exception as e:
            logger.debug(f"cancel_all_orders: fetch_open_orders failed: {e}")
            return
        for o in orders or []:
            oid = o.get("id")
            if not oid:
                continue
            try:
                await _retry(lambda _oid=oid: self.exchange.cancel_order(_oid, SYMBOL))
                logger.info(f"Cancelled open order {oid}")
            except Exception as e:
                logger.warning(f"cancel_order({oid}) failed: {e}")

    async def fetch_position(self) -> Optional[dict]:
        positions = await _retry(
            lambda: self.exchange.fetch_positions([SYMBOL])
        )
        for pos in positions:
            if pos.get("symbol") == SYMBOL and pos.get("contracts", 0) != 0:
                return pos
        return None

    async def fetch_last_trade_price(self) -> Optional[float]:
        try:
            trades = await _retry(
                lambda: self.exchange.fetch_my_trades(SYMBOL, limit=1)
            )
            if trades:
                return float(trades[-1]["price"])
        except Exception as e:
            logger.warning(f"fetch_last_trade_price failed: {e}")
        return None

    async def close_exchange(self) -> None:
        await self.exchange.close()
