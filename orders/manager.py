"""
orders/manager.py
Delta Exchange India order execution via ccxt.

FIXES IN THIS VERSION (v13):
══════════════════════════════════════════════════════════════════════════════

FIX-BRACKET-v13 | SL bracket always returns sl_order_id=None.

  Root cause (confirmed from live logs):
    Delta India bracket SL leg → info.order_type = "limit_order"
    Delta India bracket TP leg → info.order_type = "market_order"
    BOTH legs populate info.stop_price with their trigger price.

    v12 scanner checked:
      is_stop = "stop" in o_type   →  "stop" in "limit_order" = False  → MISS

    Then _find_tp_order_id matched the SL order (limit_order) as TP because
    abs(info.limit_price=66720.91 - tp_target=66443.85) = 277 < 500 → wrong hit.

    Result: sl_order_id=None every trade → modify_sl always skipped →
    exchange SL sits at original price → gets hit on reversals → wrong exits.

  FIX:
    1. Fetch open_orders ONCE per scan attempt (shared between SL + TP search).
    2. Remove ALL order-type checks.
    3. Classify bracket legs purely by info.stop_price proximity to
       sl_target vs tp_target. This is unambiguous because they are
       ATR-distance apart.
    4. Single helper _classify_brackets() replaces both _find_sl / _find_tp.

FIX-MODIFY-SL-v13 | edit_order unreliable for Delta India bracket limit_orders.
  Use cancel-then-replace: cancel old SL bracket order, place a new
  standalone reduce-only stop-limit order at the new SL price.
  New order ID is saved to self.sl_order_id for future modifications.

PRESERVED from v12:
  FIX-SIZE, FIX-EXIT, FIX-SYMBOL, retry logic, close_at_trail_sl
"""

import asyncio
import logging
from typing import Optional, Tuple
import ccxt.async_support as ccxt
from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY, BRACKET_SL_BUFFER,
)

logger = logging.getLogger(__name__)

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

# How many price points either side to accept as a bracket match.
# Wide enough for ATR volatility, narrow enough to avoid cross-matching.
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

        # Scan for bracket legs — retry up to 3× with growing delay because
        # Delta India creates bracket legs asynchronously after entry fill.
        self.sl_order_id = None
        self.tp_order_id = None

        for attempt in range(1, 4):
            await asyncio.sleep(0.8)
            self.sl_order_id, self.tp_order_id = await self._classify_brackets(
                is_long, sl, tp
            )
            if self.sl_order_id and self.tp_order_id:
                logger.info(
                    f"✅ Both bracket orders found on attempt {attempt} | "
                    f"sl_id={self.sl_order_id} tp_id={self.tp_order_id}"
                )
                break
            if self.sl_order_id or self.tp_order_id:
                logger.info(
                    f"Partial bracket found attempt {attempt} | "
                    f"sl={self.sl_order_id} tp={self.tp_order_id} — retrying..."
                )
            else:
                logger.warning(
                    f"No bracket orders found on attempt {attempt} — retrying..."
                )

        if not self.sl_order_id:
            logger.error(
                "❌ SL bracket order NOT found after 3 attempts. "
                "Trail SL modify will use market-close fallback. "
                "Check that info.stop_price is populated in Delta India response."
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

    # ── FIX-BRACKET-v13: single-pass bracket classifier ───────────────────────

    async def _classify_brackets(
        self,
        is_long: bool,
        sl_target: float,
        tp_target: float,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch open orders ONCE and classify both bracket legs by stop_price
        proximity — NO order-type checks.

        Delta India confirmed field layout (from live logs):
          Both SL and TP bracket legs:
            info.order_type  = "limit_order"  (SL)  or "market_order" (TP)
            info.stop_price  = trigger price  ← THIS is how we identify them
            side             = "sell" (long position) or "buy" (short position)

        Classification:
          whichever bracket order's stop_price is closest to sl_target → SL
          whichever bracket order's stop_price is closest to tp_target → TP
        """
        close_side = "sell" if is_long else "buy"

        try:
            open_orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
        except Exception as e:
            logger.error(f"_classify_brackets: fetch_open_orders failed: {e}")
            return None, None

        # Collect candidate orders on the correct close-side
        candidates = []
        for o in open_orders:
            if (o.get("side") or "").lower() != close_side:
                continue
            info = o.get("info", {})

            # stop_price is the reliable field — try every alias Delta uses
            stop_px = float(
                info.get("stop_price")       or
                info.get("trigger_price")    or
                o.get("stopPrice")           or
                o.get("triggerPrice")        or
                0
            )
            if stop_px <= 0:
                continue
            candidates.append((o, stop_px))

        # Debug log so future issues are easy to diagnose
        logger.info(
            f"_classify_brackets | side={close_side} "
            f"sl_target={sl_target:.2f} tp_target={tp_target:.2f} | "
            f"candidates: {[{'id': o['id'], 'stop_px': px, 'type': o.get('info',{}).get('order_type','')} for o, px in candidates]}"
        )

        sl_id: Optional[str] = None
        tp_id: Optional[str] = None

        for o, stop_px in candidates:
            dist_to_sl = abs(stop_px - sl_target)
            dist_to_tp = abs(stop_px - tp_target)

            if dist_to_sl < _BRACKET_SCAN_WINDOW and dist_to_sl <= dist_to_tp:
                sl_id = str(o["id"])
                logger.info(
                    f"✅ SL bracket found | id={sl_id} "
                    f"stop_price={stop_px:.2f} (target={sl_target:.2f} Δ={dist_to_sl:.2f})"
                )
            elif dist_to_tp < _BRACKET_SCAN_WINDOW:
                tp_id = str(o["id"])
                logger.info(
                    f"✅ TP bracket found | id={tp_id} "
                    f"stop_price={stop_px:.2f} (target={tp_target:.2f} Δ={dist_to_tp:.2f})"
                )

        if not sl_id:
            logger.warning(
                f"SL bracket not found | sl_target={sl_target:.2f} | "
                f"open_orders: {[{'id': x['id'], 'type': x.get('info',{}).get('order_type',''), 'side': x.get('side'), 'stop': x.get('info',{}).get('stop_price','')} for x in open_orders]}"
            )
        if not tp_id:
            logger.warning(
                f"TP bracket not found | tp_target={tp_target:.2f} | "
                f"open_orders: {[{'id': x['id'], 'type': x.get('info',{}).get('order_type',''), 'side': x.get('side'), 'stop': x.get('info',{}).get('stop_price','')} for x in open_orders]}"
            )

        return sl_id, tp_id

    # ── Modify SL ──────────────────────────────────────────────────────────────

    async def modify_sl(self, new_sl: float,
                        sl_limit_buf: Optional[float] = None) -> None:
        """
        FIX-MODIFY-SL-v13: Cancel old SL bracket order and place a fresh
        standalone reduce-only stop-limit order at the new SL price.

        This is more reliable than edit_order because Delta India bracket
        orders have order_type="limit_order" and ccxt's edit_order call
        may send the wrong type string, causing a 400 error.
        """
        if not self.position:
            logger.warning("modify_sl called but no position tracked")
            return

        is_long  = self.position["is_long"]
        buf      = sl_limit_buf if sl_limit_buf is not None else BRACKET_SL_BUFFER
        sl_limit = new_sl - buf if is_long else new_sl + buf
        sl_side  = "sell" if is_long else "buy"

        # If sl_order_id unknown, try to find it first
        if not self.sl_order_id:
            logger.warning("modify_sl: no sl_order_id — re-scanning...")
            entry_sl = self.position.get("initial_sl", new_sl)
            self.sl_order_id, _ = await self._classify_brackets(
                is_long, entry_sl, new_sl
            )
            if not self.sl_order_id:
                logger.error("modify_sl: re-scan failed — SL modify skipped")
                return

        logger.info(
            f"Modifying SL | cancelling id={self.sl_order_id} "
            f"→ new_sl={new_sl:.2f} limit={sl_limit:.2f}"
        )

        # Step 1: cancel the old bracket SL order
        old_id = self.sl_order_id
        self.sl_order_id = None
        try:
            await _retry(
                lambda oid=old_id: self.exchange.cancel_order(oid, SYMBOL)
            )
            logger.info(f"Old SL bracket cancelled | id={old_id}")
        except Exception as e:
            logger.warning(
                f"Cancel SL id={old_id} failed (may already be filled): {e}"
            )

        # Step 2: place a new standalone stop-limit reduce-only order
        try:
            new_order = await _retry(lambda: self.exchange.create_order(
                symbol = SYMBOL,
                type   = "stop_limit",
                side   = sl_side,
                amount = ALERT_QTY,
                price  = sl_limit,
                params = {
                    "stop_price" : new_sl,
                    "reduce_only": True,
                    "time_in_force": "gtc",
                }
            ))
            self.sl_order_id = str(new_order["id"])
            logger.info(
                f"New SL order placed | id={self.sl_order_id} "
                f"stop={new_sl:.2f} limit={sl_limit:.2f}"
            )
        except Exception as e:
            logger.error(
                f"New SL order placement failed: {e} — "
                f"position has no exchange SL. Trail loop will close via market."
            )

    # ── Trail SL exit (cancel brackets → market close) ─────────────────────────

    async def close_at_trail_sl(self, reason: str = "Trail SL") -> dict:
        if not self.position:
            logger.warning("close_at_trail_sl called but no position tracked")
            return {}

        is_long = self.position["is_long"]

        for order_id, label in [(self.tp_order_id, "TP"), (self.sl_order_id, "SL")]:
            if order_id:
                try:
                    await _retry(
                        lambda oid=order_id: self.exchange.cancel_order(oid, SYMBOL)
                    )
                    logger.info(f"Bracket {label} cancelled | id={order_id}")
                except Exception as e:
                    logger.warning(
                        f"Bracket {label} cancel failed (may be filled): {e}"
                    )

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
