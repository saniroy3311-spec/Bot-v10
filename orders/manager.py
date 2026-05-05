"""
orders/manager.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Delta Exchange order management layer used by main.py and TrailMonitor.

Public API (all async unless noted)
─────────────────────────────────────────────────────────────────────────────
  build_exchange()              → ccxt.delta  (sync factory, module-level)
  OrderManager.initialize()     → load markets, validate symbol
  OrderManager.fetch_open_position() → dict | None
  OrderManager.place_entry(is_long, sl, tp) → order dict
  OrderManager.cancel_all_orders()
  OrderManager.close_position(is_long, reason) → dict | {"info":"already_closed"}
  OrderManager.fetch_ticker()   → ccxt ticker dict
  OrderManager.close_exchange() → close ccxt session

Error handling
──────────────
  • Network / timeout errors retry up to 3× with exponential back-off.
  • close_position() returns {"info": "already_closed"} instead of raising
    when the exchange reports no position to reduce (FIX-OM-003/005).
    TrailMonitor._fire_exit() treats this as a success — no retry storm.
  • All methods log failures but never silently swallow them outside the
    explicit retry/fallback paths.

Delta Exchange endpoints
────────────────────────
  Live:    https://api.india.delta.exchange
  Testnet: https://testnet-api.india.delta.exchange
  Toggle:  DELTA_TESTNET=true in .env
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import ccxt.async_support as ccxt

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY,
)

logger = logging.getLogger("orders.manager")

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

# Phrases in ccxt exception messages that mean "position is already gone"
_ALREADY_CLOSED_PHRASES = (
    "no_position_for_reduce_only",
    "no open position",
    "position not found",
    "insufficient position",
)


# ─── Exchange factory ──────────────────────────────────────────────────────────

def build_exchange() -> ccxt.delta:
    """
    Build a ccxt.delta async instance pointed at Delta India.
    Called once at startup; the same session is reused throughout.
    """
    base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
    return ccxt.delta({
        "apiKey":          DELTA_API_KEY,
        "secret":          DELTA_API_SECRET,
        "enableRateLimit": True,
        "urls": {
            "api": {
                "public":  base_url,
                "private": base_url,
            }
        },
    })


# ─── Retry helper ─────────────────────────────────────────────────────────────

async def _retry(coro_fn, retries: int = 3, delay: float = 1.0):
    """
    Retry a coroutine-producing callable on network / timeout errors.
    Uses exponential back-off: 1s, 2s, 4s.
    """
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            if attempt == retries:
                raise
            wait = delay * (2 ** (attempt - 1))
            logger.warning(
                f"[OM] Retry {attempt}/{retries} after {wait:.1f}s — {exc}"
            )
            await asyncio.sleep(wait)


# ─── OrderManager ─────────────────────────────────────────────────────────────

class OrderManager:
    """
    Async Delta Exchange order manager.

    Instantiated once in main.py's ShivaSniperBot and shared with TrailMonitor.
    """

    def __init__(self) -> None:
        self.exchange: ccxt.delta = build_exchange()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load markets and validate the configured symbol exists."""
        await self.exchange.load_markets()
        if SYMBOL not in self.exchange.markets:
            raise ValueError(
                f"SYMBOL '{SYMBOL}' not found on Delta India. "
                f"Available symbols include: "
                f"{list(self.exchange.markets.keys())[:10]}"
            )
        logger.info(f"[OM] Initialized — symbol={SYMBOL}  qty={ALERT_QTY}")

    async def close_exchange(self) -> None:
        """Close the ccxt session (called on shutdown)."""
        try:
            await self.exchange.close()
        except Exception as exc:
            logger.warning(f"[OM] close_exchange error (ignored): {exc}")

    # ── Position query ────────────────────────────────────────────────────────

    async def fetch_open_position(self) -> Optional[dict]:
        """
        Return a simplified position dict if an open position exists, else None.

        Return schema: {"is_long": bool, "entry_price": float, "contracts": float}

        Used only in the startup recovery path in main.py — not called
        during normal bar-close / trail operation.
        """
        try:
            positions = await _retry(
                lambda: self.exchange.fetch_positions([SYMBOL])
            )
            for pos in positions:
                size = float(pos.get("contracts", 0) or 0)
                if abs(size) > 0 and pos.get("symbol") == SYMBOL:
                    side      = pos.get("side", "long").lower()
                    is_long   = side == "long"
                    entry_raw = (
                        pos.get("entryPrice")
                        or (pos.get("info") or {}).get("entry_price")
                        or 0.0
                    )
                    return {
                        "is_long":     is_long,
                        "entry_price": float(entry_raw),
                        "contracts":   abs(size),
                    }
        except Exception as exc:
            logger.warning(f"[OM] fetch_open_position failed: {exc}")
        return None

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_entry(
        self,
        is_long: bool,
        sl: float,
        tp: float,
    ) -> dict:
        """
        Place a market entry order (SL and TP are managed by TrailMonitor,
        not as bracket orders — Delta India bracket orders are not reliable
        on all products).

        Returns the ccxt order dict. Raises on failure (caller handles).
        """
        side = "buy" if is_long else "sell"
        logger.info(
            f"[OM] Placing entry | side={side}  qty={ALERT_QTY}  "
            f"sl={sl:.2f}  tp={tp:.2f}"
        )
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
        ))
        fill = float(order.get("average") or order.get("price") or 0.0)
        logger.info(
            f"[OM] Entry filled | id={order.get('id')}  fill={fill:.2f}"
        )
        return order

    # ── Order management ──────────────────────────────────────────────────────

    async def cancel_all_orders(self) -> None:
        """
        Cancel all open orders for the symbol.
        Never raises — failures are logged and swallowed (best-effort cleanup).
        """
        try:
            await _retry(lambda: self.exchange.cancel_all_orders(SYMBOL))
            logger.debug("[OM] cancel_all_orders: done")
        except Exception as exc:
            logger.warning(f"[OM] cancel_all_orders failed (ignored): {exc}")

    async def close_position(
        self,
        is_long: bool,
        reason: str = "Exit",
    ) -> dict:
        """
        Close the open position with a reduce-only market order.

        FIX-OM-003 / FIX-OM-005:
        If the exchange reports no position to reduce (position already closed
        by TP bracket, liquidation, or manual action), return
        {"info": "already_closed"} instead of raising. TrailMonitor treats
        this as a success to prevent the infinite-retry cascade.

        All other exchange errors are re-raised for the caller (TrailMonitor)
        to handle with its own bounded retry logic (FIX-TRAIL-04).
        """
        side = "sell" if is_long else "buy"
        logger.info(
            f"[OM] Closing position | side={side}  reason={reason}"
        )
        try:
            order = await _retry(lambda: self.exchange.create_order(
                symbol = SYMBOL,
                type   = "market",
                side   = side,
                amount = ALERT_QTY,
                params = {"reduce_only": True},
            ))
            fill = float(order.get("average") or order.get("price") or 0.0)
            logger.info(
                f"[OM] Position closed | id={order.get('id')}  fill={fill:.2f}"
            )
            return order
        except ccxt.ExchangeError as exc:
            msg = str(exc).lower()
            if any(phrase in msg for phrase in _ALREADY_CLOSED_PHRASES):
                logger.info(
                    f"[OM] close_position: exchange says position already gone "
                    f"({exc}) — returning already_closed sentinel"
                )
                return {"info": "already_closed"}
            raise

    # ── Price feed (safety-net REST poll) ────────────────────────────────────

    async def fetch_ticker(self) -> Optional[dict]:
        """
        Fetch the current ticker for the symbol.

        Used by TrailMonitor._get_mark_price() as a 2-second safety-net
        fallback when the WS candle stream is not delivering price ticks.

        Key priority for mark price (FIX-AUDIT-01):
          1. ticker["markPrice"]            — ccxt normalised
          2. ticker["info"]["mark_price"]   — raw Delta field
          3. ticker["last"]                 — last traded price
        """
        try:
            ticker = await _retry(lambda: self.exchange.fetch_ticker(SYMBOL))
            return ticker
        except Exception as exc:
            logger.warning(f"[OM] fetch_ticker failed: {exc}")
            return None
