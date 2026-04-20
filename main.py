"""
orders/manager.py  —  Shiva Sniper v6.5  (BRACKET MODE)
Market entry with exchange-side bracket SL/TP as hard floor.
Python trail_loop.py manages dynamic trailing on top.
"""

import asyncio
import logging
from typing import Optional

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
    logger.info(
        f"Exchange built → {'TESTNET' if DELTA_TESTNET else 'LIVE'} | "
        f"endpoint={base_url} | qty={ALERT_QTY} lots | mode=BRACKET"
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

    async def place_entry(self, is_long: bool, sl: float, tp: float) -> dict:
        side = "buy" if is_long else "sell"
        logger.info(
            f"Placing {side.upper()} entry (bracket) | "
            f"SL={sl:.2f} TP={tp:.2f} | Qty={ALERT_QTY} lots"
        )
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
            params = {
                "stop_loss_order": {
                    "order_type": "market_order",
                    "stop_price": round(sl, 1),
                },
                "take_profit_order": {
                    "order_type": "market_order",
                    "stop_price": round(tp, 1),
                },
            }
        ))
        fill_price = float(order.get("average") or order.get("price") or 0)
        self.position = {
            "entry_order_id": order["id"],
            "is_long"       : is_long,
            "entry_price"   : fill_price,
        }
        logger.info(
            f"Entry filled | price={fill_price:.2f} "
            f"| SL={sl:.2f} TP={tp:.2f} — exchange bracket + Python trail active ✅"
        )
        return order

    async def modify_sl(self, new_sl: float,
                        sl_limit_buf: Optional[float] = None) -> None:
        logger.debug(f"modify_sl({new_sl:.2f}) — bracket mode, exchange handles hard floor")

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

    async def close_position(self, reason: str = "Max SL Hit") -> dict:
        return await self.close_at_trail_sl(reason=reason)

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
