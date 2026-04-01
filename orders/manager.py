"""
orders/manager.py
Delta Exchange India order execution via ccxt.

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-SL1  | sl_order_id was never captured.
           Delta India returns the bracket SL as a SEPARATE open order —
           it is NOT embedded in the entry response. The entry response
           `info` dict contains only the market entry order itself.
           FIX: After entry fills, always call _find_sl_order_id() which
           fetches open orders and matches the bracket stop leg by side +
           stop price proximity. This is the ONLY reliable way on Delta India.

FIX-SL2  | _find_sl_order_id() price proximity window was 200 pts.
           With ATR ~120, the initial SL is ~72 pts from entry, so 200 is
           fine — but we now also match on order type "stop_market" to
           avoid confusing the bracket TP limit order with the SL.

FIX-EXIT | Trail-exit via market order when trail SL is breached.
           Previously the bot relied on the bracket TP firing on exchange.
           Pine Script exits via trailing SL cross — this method matches.
           New method: close_position_at_trail_sl() — called by trail_loop
           when current_price crosses state.current_sl.
           The bracket TP and SL orders are cancelled first, then a
           reduce-only market order closes the position.
"""

import asyncio
import logging
from typing import Optional
import ccxt.async_support as ccxt
from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY, BRACKET_SL_BUFFER,
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
        "urls": {
            "api": {
                "public" : base_url,
                "private": base_url,
            }
        },
    }
    exchange = ccxt.delta(params)
    logger.info(
        f"Exchange built → {'TESTNET' if DELTA_TESTNET else 'LIVE'} | "
        f"endpoint={base_url} | qty={ALERT_QTY}"
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
        self.exchange      = build_exchange()
        self.position      : Optional[dict] = None
        self.sl_order_id   : Optional[str]  = None
        self.tp_order_id   : Optional[str]  = None

    # ── Entry ─────────────────────────────────────────────────────────────────
    async def place_entry(self, is_long: bool, sl: float, tp: float) -> dict:
        """
        Market entry + OCO bracket (TP limit + SL stop).

        FIX-SL1: After entry fills, ALWAYS scan open orders to find the
        bracket SL order ID. Delta India never returns it in the entry
        response — it is created as a separate open order.
        """
        side     = "buy" if is_long else "sell"
        sl_limit = sl - BRACKET_SL_BUFFER if is_long else sl + BRACKET_SL_BUFFER
        logger.info(
            f"Placing {side.upper()} entry | "
            f"SL={sl:.2f} SL_limit={sl_limit:.2f} TP={tp:.2f} Qty={ALERT_QTY}"
        )

        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
            params = {
                "bracket_stop_loss_price"       : sl,
                "bracket_stop_loss_limit_price" : sl_limit,
                "bracket_take_profit_price"     : tp,
            }
        ))

        logger.info(f"Entry order response: {order}")

        # FIX-SL1: Always scan for SL — never trust the entry response to contain it
        # Wait 1s for Delta to create the bracket legs before scanning
        await asyncio.sleep(1.0)
        self.sl_order_id = await self._find_sl_order_id(is_long, sl)
        self.tp_order_id = await self._find_tp_order_id(is_long, tp)

        fill_price = float(order.get("average") or order.get("price") or 0)
        self.position = {
            "entry_order_id": order["id"],
            "is_long"       : is_long,
            "entry_price"   : fill_price,
        }
        logger.info(
            f"Entry filled | price={fill_price:.2f} "
            f"sl_order_id={self.sl_order_id} tp_order_id={self.tp_order_id}"
        )
        return order

    async def _find_sl_order_id(self, is_long: bool,
                                 sl_price: float) -> Optional[str]:
        """
        FIX-SL2: Scan open orders for the bracket stop leg.
        Match on: correct side + stop-type + price within 200 pts.
        """
        try:
            open_orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
            sl_side = "sell" if is_long else "buy"
            for o in open_orders:
                o_type  = (o.get("type") or "").lower()
                o_side  = (o.get("side") or "").lower()
                # stopPrice or triggerPrice depending on ccxt normalisation
                o_price = float(
                    o.get("stopPrice") or
                    o.get("triggerPrice") or
                    o.get("info", {}).get("stop_price") or
                    o.get("price") or 0
                )
                is_stop = "stop" in o_type or "trigger" in o_type
                if o_side == sl_side and is_stop and abs(o_price - sl_price) < 200:
                    logger.info(f"SL order found | id={o['id']} price={o_price}")
                    return str(o["id"])
            logger.error("Could not find bracket SL order — trail SL modify will use market close fallback")
            return None
        except Exception as e:
            logger.error(f"Failed to scan for SL order: {e}")
            return None

    async def _find_tp_order_id(self, is_long: bool,
                                 tp_price: float) -> Optional[str]:
        """Scan open orders for the bracket take-profit leg."""
        try:
            open_orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
            tp_side = "sell" if is_long else "buy"
            for o in open_orders:
                o_type  = (o.get("type") or "").lower()
                o_side  = (o.get("side") or "").lower()
                o_price = float(
                    o.get("price") or
                    o.get("info", {}).get("limit_price") or 0
                )
                is_limit = "limit" in o_type and "stop" not in o_type
                if o_side == tp_side and is_limit and abs(o_price - tp_price) < 200:
                    logger.info(f"TP order found | id={o['id']} price={o_price}")
                    return str(o["id"])
            logger.warning("Could not find bracket TP order")
            return None
        except Exception as e:
            logger.error(f"Failed to scan for TP order: {e}")
            return None

    # ── Modify SL ─────────────────────────────────────────────────────────────
    async def modify_sl(self, new_sl: float,
                        sl_limit_buf: Optional[float] = None) -> None:
        """
        Modify the bracket stop loss on Delta Exchange India.
        If sl_order_id is missing, re-scan before giving up.
        """
        if not self.position:
            logger.warning("modify_sl called but no position tracked")
            return

        # Re-scan if we lost the SL order ID
        if not self.sl_order_id:
            logger.warning("modify_sl: no sl_order_id — attempting re-scan")
            is_long = self.position["is_long"]
            self.sl_order_id = await self._find_sl_order_id(is_long, new_sl)
            if not self.sl_order_id:
                logger.error("modify_sl: re-scan failed — SL modify skipped this tick")
                return

        is_long = self.position["is_long"]
        buf      = sl_limit_buf if sl_limit_buf is not None else BRACKET_SL_BUFFER
        sl_limit = new_sl - buf if is_long else new_sl + buf
        sl_side  = "sell" if is_long else "buy"

        logger.info(
            f"Modifying SL | id={self.sl_order_id} "
            f"new_sl={new_sl:.2f} limit={sl_limit:.2f} buf={buf:.2f}"
        )

        try:
            await _retry(lambda: self.exchange.edit_order(
                id     = self.sl_order_id,
                symbol = SYMBOL,
                type   = "stop",
                side   = sl_side,
                amount = ALERT_QTY,
                price  = sl_limit,
                params = {
                    "stopPrice"  : new_sl,
                    "reduce_only": True,
                }
            ))
        except Exception as e:
            logger.error(
                f"SL modify failed (id={self.sl_order_id}): {e} — "
                f"re-scanning for SL order"
            )
            # Clear stale ID and re-scan next tick
            self.sl_order_id = None

    # ── FIX-EXIT: Trail SL breach → cancel brackets → market close ────────────
    async def close_at_trail_sl(self, reason: str = "Trail SL") -> dict:
        """
        FIX-EXIT: Replicate Pine Script trail SL exit behaviour.

        When trail_loop detects price has crossed state.current_sl:
          1. Cancel the bracket TP order (prevents double-fill)
          2. Cancel the bracket SL order (prevents double-fill)
          3. Send reduce-only market order to close position

        This matches Pine's strategy.exit() trail behaviour exactly:
        Pine closes at the bar where price crosses the trailing stop.
        The bot closes via market order the moment price crosses current_sl
        in the tick loop (TRAIL_LOOP_SEC resolution).
        """
        if not self.position:
            logger.warning("close_at_trail_sl called but no position tracked")
            return {}

        is_long = self.position["is_long"]

        # Step 1: Cancel TP bracket to prevent double-fill
        if self.tp_order_id:
            try:
                await _retry(lambda: self.exchange.cancel_order(
                    self.tp_order_id, SYMBOL
                ))
                logger.info(f"Bracket TP cancelled | id={self.tp_order_id}")
            except Exception as e:
                logger.warning(f"TP cancel failed (may already be filled): {e}")

        # Step 2: Cancel SL bracket to prevent double-fill
        if self.sl_order_id:
            try:
                await _retry(lambda: self.exchange.cancel_order(
                    self.sl_order_id, SYMBOL
                ))
                logger.info(f"Bracket SL cancelled | id={self.sl_order_id}")
            except Exception as e:
                logger.warning(f"SL cancel failed (may already be filled): {e}")

        # Step 3: Market close
        side = "sell" if is_long else "buy"
        logger.info(f"Trail SL exit ({reason}) | side={side} qty={ALERT_QTY}")

        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
            params = {"reduce_only": True}
        ))

        self.position    = None
        self.sl_order_id = None
        self.tp_order_id = None
        return order

    # ── Emergency close (Max SL) ───────────────────────────────────────────────
    async def close_position(self, reason: str = "Max SL Hit") -> dict:
        """Emergency close — cancel brackets then market close."""
        return await self.close_at_trail_sl(reason=reason)

    # ── Fetch position ────────────────────────────────────────────────────────
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
