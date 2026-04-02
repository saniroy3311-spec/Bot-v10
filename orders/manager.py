"""
orders/manager.py
Delta Exchange India order execution via ccxt.

FIXES IN THIS VERSION (v12):
══════════════════════════════════════════════════════════════════════════════

FIX-BRACKET | Bracket SL/TP orders not found after entry.
  From live logs:
    "Could not find bracket SL order — trail SL modify will use market close fallback"
    "Could not find bracket TP order"
    sl_order_id=None  tp_order_id=None

  Root cause: Delta India creates bracket legs as SEPARATE orders with
  these exact type strings in the API response:
    SL leg:  order_type = "stop_market_order"   (not "stop_market" or "stop")
    TP leg:  order_type = "limit_order"         (not "limit")

  The old scanner did:
    is_stop = "stop" in o_type        ← ✅ would match "stop_market_order"
    is_limit = "limit" in o_type AND "stop" not in o_type  ← ❌ FAILED
    because "stop_market_order" contains "order" but the TP leg also has
    "limit_order" which contains "order". The price proximity check was
    also narrow (200 pts) and the bracket legs use stop_price / limit_price
    fields inside info{} not the top-level ccxt unified fields.

  FIX:
    1. Always scan using info{} raw fields (stop_price, limit_price)
    2. Match SL by: side correct + info.stop_price within 300 pts of our SL
    3. Match TP by: side correct + info.limit_price within 300 pts of our TP
    4. Widen proximity to 500 pts to handle ATR volatility
    5. After 1s wait (current), also retry up to 3 times with 0.5s gap
       because Delta sometimes creates bracket legs with ~1-2s delay

FIX-SIZE | Order size=1 in logs — correct for 1 lot on Delta India.
  qty=1 lot = 0.001 BTC. All P/L calculations use lots_to_btc() from
  risk/calculator.py. Telegram messages show both lots and BTC.

PRESERVED FIXES from v10:
  FIX-SL1:   sl_order_id never captured (fixed by scan)
  FIX-EXIT:  Trail-exit via cancel brackets + market close
  FIX-SYMBOL: load_markets() on initialize()
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

# Proximity window for bracket order scan (pts from target price)
_BRACKET_SCAN_WINDOW = 500


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
        f"endpoint={base_url} | qty={ALERT_QTY} lots"
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
        self.exchange    = build_exchange()
        self.position    : Optional[dict] = None
        self.sl_order_id : Optional[str]  = None
        self.tp_order_id : Optional[str]  = None

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

    # ── Entry ──────────────────────────────────────────────────────────────────
    async def place_entry(self, is_long: bool, sl: float, tp: float) -> dict:
        """
        Market entry + OCO bracket (TP limit + SL stop).

        FIX-BRACKET: After entry fills, scan for bracket legs up to 3 times
        with 0.5s gap each, because Delta India creates them asynchronously
        and they may not be visible immediately after the entry fill.
        """
        side     = "buy" if is_long else "sell"
        sl_limit = sl - BRACKET_SL_BUFFER if is_long else sl + BRACKET_SL_BUFFER
        logger.info(
            f"Placing {side.upper()} entry | "
            f"SL={sl:.2f} SL_limit={sl_limit:.2f} TP={tp:.2f} Qty={ALERT_QTY} lots"
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

        # FIX-BRACKET: retry bracket scan up to 3 times
        self.sl_order_id = None
        self.tp_order_id = None
        for attempt in range(1, 4):
            await asyncio.sleep(0.8)
            self.sl_order_id = await self._find_sl_order_id(is_long, sl)
            self.tp_order_id = await self._find_tp_order_id(is_long, tp)
            if self.sl_order_id and self.tp_order_id:
                logger.info(
                    f"Bracket orders found on attempt {attempt} | "
                    f"sl_id={self.sl_order_id} tp_id={self.tp_order_id}"
                )
                break
            if self.sl_order_id or self.tp_order_id:
                logger.info(
                    f"Partial bracket found attempt {attempt} | "
                    f"sl={self.sl_order_id} tp={self.tp_order_id} — retrying..."
                )
            else:
                logger.warning(f"No bracket orders found on attempt {attempt} — retrying...")

        if not self.sl_order_id:
            logger.error(
                "❌ SL bracket order NOT found after 3 attempts. "
                "Trail SL will use market-close fallback. "
                "Check Delta India API — bracket legs may have different product_id."
            )
        if not self.tp_order_id:
            logger.warning("⚠️ TP bracket order NOT found after 3 attempts.")

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
        FIX-BRACKET: Scan open orders for the bracket stop-loss leg.

        Delta India bracket SL order characteristics:
          - order_type: "stop_market_order"
          - side: "sell" for long positions, "buy" for short positions
          - stop_price (in info dict): close to our sl_price
          - product_symbol: "BTCUSD" (matches our order)

        We scan info{} directly because ccxt unified fields may not
        populate stop_price from Delta India's response format.
        """
        try:
            open_orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
            sl_side = "sell" if is_long else "buy"

            for o in open_orders:
                o_side = (o.get("side") or "").lower()
                if o_side != sl_side:
                    continue

                info = o.get("info", {})
                o_type = (
                    info.get("order_type") or
                    o.get("type") or ""
                ).lower()

                # Delta India SL bracket = stop_market_order
                is_stop = "stop" in o_type

                # Get stop price from multiple possible fields
                stop_px = float(
                    info.get("stop_price") or
                    info.get("trigger_price") or
                    o.get("stopPrice") or
                    o.get("triggerPrice") or
                    o.get("price") or 0
                )

                if is_stop and stop_px > 0 and abs(stop_px - sl_price) < _BRACKET_SCAN_WINDOW:
                    logger.info(
                        f"✅ SL bracket found | id={o['id']} "
                        f"type={o_type} stop_price={stop_px:.2f} "
                        f"(target={sl_price:.2f})"
                    )
                    return str(o["id"])

            # Log all open orders for debugging
            logger.warning(
                f"SL bracket not found | sl_target={sl_price:.2f} | "
                f"open_orders: {[{'id':x['id'],'type':(x.get('info',{}).get('order_type','')),'side':x.get('side'),'stop':(x.get('info',{}).get('stop_price',''))} for x in open_orders]}"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to scan for SL bracket: {e}")
            return None

    async def _find_tp_order_id(self, is_long: bool,
                                  tp_price: float) -> Optional[str]:
        """
        FIX-BRACKET: Scan open orders for the bracket take-profit leg.

        Delta India bracket TP order characteristics:
          - order_type: "limit_order"
          - side: "sell" for long, "buy" for short
          - limit_price (in info dict): close to our tp_price
        """
        try:
            open_orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
            tp_side = "sell" if is_long else "buy"

            for o in open_orders:
                o_side = (o.get("side") or "").lower()
                if o_side != tp_side:
                    continue

                info   = o.get("info", {})
                o_type = (
                    info.get("order_type") or
                    o.get("type") or ""
                ).lower()

                # Delta India TP bracket = limit_order (NOT stop)
                is_limit = "limit" in o_type and "stop" not in o_type

                # Get limit price
                limit_px = float(
                    info.get("limit_price") or
                    o.get("price") or 0
                )

                if is_limit and limit_px > 0 and abs(limit_px - tp_price) < _BRACKET_SCAN_WINDOW:
                    logger.info(
                        f"✅ TP bracket found | id={o['id']} "
                        f"type={o_type} limit_price={limit_px:.2f} "
                        f"(target={tp_price:.2f})"
                    )
                    return str(o["id"])

            logger.warning(
                f"TP bracket not found | tp_target={tp_price:.2f} | "
                f"open_orders: {[{'id':x['id'],'type':(x.get('info',{}).get('order_type','')),'side':x.get('side'),'price':x.get('price')} for x in open_orders]}"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to scan for TP bracket: {e}")
            return None

    # ── Modify SL ──────────────────────────────────────────────────────────────
    async def modify_sl(self, new_sl: float,
                        sl_limit_buf: Optional[float] = None) -> None:
        if not self.position:
            logger.warning("modify_sl called but no position tracked")
            return

        if not self.sl_order_id:
            logger.warning("modify_sl: no sl_order_id — re-scanning...")
            is_long = self.position["is_long"]
            self.sl_order_id = await self._find_sl_order_id(is_long, new_sl)
            if not self.sl_order_id:
                logger.error("modify_sl: re-scan failed — SL modify skipped")
                return

        is_long  = self.position["is_long"]
        buf      = sl_limit_buf if sl_limit_buf is not None else BRACKET_SL_BUFFER
        sl_limit = new_sl - buf if is_long else new_sl + buf
        sl_side  = "sell" if is_long else "buy"

        logger.info(
            f"Modifying SL | id={self.sl_order_id} "
            f"new_sl={new_sl:.2f} limit={sl_limit:.2f}"
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
                f"SL modify failed (id={self.sl_order_id}): {e} — re-scanning"
            )
            self.sl_order_id = None

    # ── Trail SL exit (cancel brackets → market close) ─────────────────────────
    async def close_at_trail_sl(self, reason: str = "Trail SL") -> dict:
        if not self.position:
            logger.warning("close_at_trail_sl called but no position tracked")
            return {}

        is_long = self.position["is_long"]

        for order_id, label in [(self.tp_order_id, "TP"), (self.sl_order_id, "SL")]:
            if order_id:
                try:
                    await _retry(lambda oid=order_id: self.exchange.cancel_order(oid, SYMBOL))
                    logger.info(f"Bracket {label} cancelled | id={order_id}")
                except Exception as e:
                    logger.warning(f"Bracket {label} cancel failed (may be filled): {e}")

        side = "sell" if is_long else "buy"
        logger.info(f"Trail SL exit ({reason}) | side={side} qty={ALERT_QTY} lots")

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
