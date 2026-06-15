"""
orders/manager.py — Shiva Sniper Bot-v10  |  EMERGENCY-BRACKET ARCHITECTURE
══════════════════════════════════════════════════════════════════════════════

ARCHITECTURE (FIX-BRACKET-CHURN):
─────────────────────────────────────────────────────────────────────────────
Previous design pushed every trail SL tighten to Delta via PUT /v2/orders/bracket.
Delta internally replaces the order on each amendment, issuing a new order ID.
The bot's cached _bracket_order_id became stale on every update, triggering a
continuous open_order_not_found → rediscovery loop (~3 API calls per tick for
the entire duration of the trade).

New design:
  • Bracket is placed ONCE at entry with the INITIAL SL only (wide safety net).
  • Bracket is NEVER amended after placement.
  • Python (TrailMonitor) owns all trail/BE/tighten logic and fires exits via
    market close_position() on tick.
  • The bracket's only job is crash/disconnect protection — if the bot dies,
    Delta's bracket catches the worst-case initial SL. No stale IDs, no
    amendment API calls, no rediscovery loops.

API surface used:
  POST  /v2/orders/bracket   place emergency SL bracket after entry fill
  DELETE /v2/orders/bracket  cancel bracket when Python fires a clean exit

Delta Exchange endpoints
────────────────────────
  Live:    https://api.india.delta.exchange
  Testnet: https://testnet-api.india.delta.exchange
  Toggle:  DELTA_TESTNET=true in .env
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import ssl
import time
import socket
from typing import Any, Optional

import aiohttp
import ccxt.async_support as ccxt

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY,
    BRACKET_SL_WIDEN_MULT, BRACKET_SL_MIN_PTS, MAX_SL_POINTS,
)

logger = logging.getLogger("orders.manager")

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

# Phrases in ccxt / Delta error messages that mean "position is already gone"
_ALREADY_CLOSED_PHRASES = (
    "no_position_for_reduce_only",
    "no open position",
    "position not found",
    "insufficient position",
)

# Phrases that mean "bracket is already gone" (already triggered or removed)
_BRACKET_GONE_PHRASES = (
    "bracket_not_found",
    "no_bracket",
    "no bracket order",
    "bracket order not found",
    "no_open_bracket_order_for_position",
)


# ─── Exchange factory ──────────────────────────────────────────────────────────

def build_exchange() -> ccxt.delta:
    """
    Build a ccxt.delta async instance pointed at Delta India.
    Pre-injects an IPv4-only session to bypass dual-stack IPv6 errors.
    """
    base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
    ex = ccxt.delta({
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
    
    _ssl_ctx   = ssl.create_default_context()
    _connector = aiohttp.TCPConnector(
        family=socket.AF_INET,
        ssl=_ssl_ctx,
        enable_cleanup_closed=True,
    )
    ex.session    = aiohttp.ClientSession(connector=_connector)
    ex.own_session = False   
    return ex


# ─── Retry helper ─────────────────────────────────────────────────────────────

async def _retry(coro_fn, retries: int = 3, delay: float = 1.0):
    """Retry a coroutine-producing callable on network / timeout errors."""
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


# ─── Delta India signed REST helper (for bracket endpoints) ───────────────────

def _sign(method: str, ts: str, path: str, body: str) -> str:
    msg = (method + ts + path + body).encode()
    return hmac.new(DELTA_API_SECRET.encode(), msg, hashlib.sha256).hexdigest()


async def _signed_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    body_obj: Optional[dict] = None,
) -> dict:
    """Make a signed HTTP request to Delta India for endpoints not in ccxt."""
    base   = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
    url    = base + path
    body   = json.dumps(body_obj) if body_obj is not None else ""
    ts     = str(int(time.time()))
    sig    = _sign(method, ts, path, body)
    headers = {
        "api-key":      DELTA_API_KEY,
        "signature":    sig,
        "timestamp":    ts,
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "shiva-sniper-bot-v10",
    }
    async with session.request(method, url, data=body, headers=headers, timeout=10) as resp:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"_raw": text}
        if resp.status >= 400:
            raise ccxt.ExchangeError(
                f"Delta {method} {path} returned {resp.status}: {text}"
            )
        return data


# ─── OrderManager ─────────────────────────────────────────────────────────────

class OrderManager:
    """Async Delta Exchange order manager with Phase-2 bracket-order support."""

    def __init__(self) -> None:
        self.exchange: ccxt.delta = build_exchange()

        # PHASE-2 state — set on entry fill, cleared on exit.
        self._product_id:    Optional[int]   = None  
        self._product_symbol: Optional[str]  = None  
        self._bracket_active:        bool    = False
        self._current_sl:    Optional[float] = None
        self._current_tp:    Optional[float] = None
        self._is_long:       Optional[bool]  = None  

        # Reusable HTTP session for the signed-bracket endpoints.
        self._http: Optional[aiohttp.ClientSession] = None
        
        # Strong references to prevent background tasks from being garbage collected
        self._background_tasks: set[asyncio.Task] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load markets and validate the configured symbol exists."""
        await self.exchange.load_markets()
        if SYMBOL not in self.exchange.markets:
            raise ValueError(f"SYMBOL '{SYMBOL}' not found on Delta India.")

        market = self.exchange.markets[SYMBOL]
        info   = market.get("info") or {}
        pid    = info.get("id") or info.get("product_id") or market.get("id")
        
        try:
            self._product_id = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            self._product_id = None
        self._product_symbol = info.get("symbol") or "BTCUSD"

        if self._product_id is None:
            logger.warning(
                f"[OM] Could not resolve numeric product_id for {SYMBOL}; "
                f"bracket orders will be DISABLED for this run."
            )
        else:
            logger.info(
                f"[OM] Resolved product_id={self._product_id} "
                f"product_symbol={self._product_symbol}"
            )

    async def close_exchange(self) -> None:
        """Close the ccxt session and the bracket-endpoint HTTP session."""
        try:
            await self.exchange.close()
        except Exception as exc:
            logger.warning(f"[OM] close_exchange error (ignored): {exc}")
        if self._http is not None:
            try:
                await self._http.close()
            except Exception as exc:
                logger.warning(f"[OM] http session close error (ignored): {exc}")
            self._http = None

    async def _http_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session for bracket endpoints."""
        if self._http is None or self._http.closed:
            _connector = aiohttp.TCPConnector(family=socket.AF_INET)
            self._http = aiohttp.ClientSession(connector=_connector)
        return self._http

    # ── Position query ────────────────────────────────────────────────────────

    async def fetch_open_position(self) -> Optional[dict]:
        """Return a simplified position dict if an open position exists, else None."""
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

    async def fetch_position(self) -> Optional[dict]:
        """Backward-compatibility wrapper for legacy layout logic execution passes."""
        return await self.fetch_open_position()

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_entry(
        self,
        is_long: bool,
        sl: float,
        tp: float,
    ) -> dict:
        """
        Place market entry order, then attach an WIDE EMERGENCY bracket SL.
        Python trail loop retains complete operational ownership of exits.
        """
        side = "buy" if is_long else "sell"
        logger.info(
            f"[OM] Placing entry | side={side}  qty={ALERT_QTY}  "
            f"sl={sl:.2f}  tp={tp:.2f}"
        )

        # ── 1. Market entry ──
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
        ))
        fill = float(order.get("average") or order.get("price") or 0.0)
        logger.info(f"[OM] Entry filled | id={order.get('id')}  fill={fill:.2f}")

        # ── 2. Cache state ──
        self._is_long          = is_long
        self._current_sl       = float(sl)
        self._current_tp       = float(tp)
        self._bracket_active   = False

        # ── 3. Emergency bracket SL (Widen by 300 points to absorb normal wick noise) ──
        if self._product_id is None:
            logger.warning("[OM] Emergency bracket disabled (no product_id).")
            return order

        try:
            # Dynamic emergency bracket: clamp(sl_dist * WIDEN_MULT, MIN_PTS, MAX_SL_POINTS)
            # Uses config.py values so it scales with volatility and is tunable via .env
            sl_dist = abs(sl - fill) if fill > 0 else BRACKET_SL_MIN_PTS
            bracket_dist = max(sl_dist * BRACKET_SL_WIDEN_MULT, BRACKET_SL_MIN_PTS)
            bracket_dist = min(bracket_dist, MAX_SL_POINTS)
            emergency_sl = (fill - bracket_dist) if is_long else (fill + bracket_dist)
            
            await self._place_bracket(sl=emergency_sl)
            self._bracket_active = True
            logger.info(
                f"[OM] ✅ Emergency bracket SL placed on Delta | "
                f"sl={emergency_sl:.2f}  (fill={fill:.2f}  pine_sl={sl:.2f}  "                f"sl_dist={sl_dist:.1f}  bracket_dist={bracket_dist:.1f}  "                f"widen={BRACKET_SL_WIDEN_MULT}x  min={BRACKET_SL_MIN_PTS}  max={MAX_SL_POINTS})"
            )
        except Exception as exc:
            logger.error(
                f"[OM] ⚠️  Emergency bracket FAILED — trade is open with no "
                f"exchange-side safety net. TrailMonitor is sole protection. Error: {exc}"
            )

        return order

    # ── Bracket management ─────────────────────────────────────────────────────

    async def _place_bracket(self, sl: float) -> dict:
        """POST /v2/orders/bracket — emergency SL only, no TP."""
        body = {
            "product_id":     self._product_id,
            "product_symbol": self._product_symbol,
            "stop_loss_order": {
                "order_type": "market_order",
                "stop_price": str(round(sl, 2)),
            },
            "bracket_stop_trigger_method": "last_traded_price",
        }
        session = await self._http_session()
        return await _signed_request(session, "POST", "/v2/orders/bracket", body)

    async def cancel_bracket(self) -> None:
        """DELETE /v2/orders/bracket — remove the safety net from Delta execution layer."""
        if not self._bracket_active or self._product_id is None:
            self._bracket_active = False
            return
        body = {
            "product_id":     self._product_id,
            "product_symbol": self._product_symbol,
        }
        session = await self._http_session()
        try:
            await _signed_request(session, "DELETE", "/v2/orders/bracket", body)
            logger.info("[OM] Bracket cancelled on Delta")
        except Exception as exc:
            msg = str(exc).lower()
            if any(p in msg for p in _BRACKET_GONE_PHRASES):
                logger.info("[OM] cancel_bracket: bracket was already gone")
            else:
                logger.warning(f"[OM] cancel_bracket failed (ignored): {exc}")
        finally:
            self._bracket_active   = False
            self._current_sl       = None
            self._current_tp       = None
            self._is_long          = None

    # ── Order management ──────────────────────────────────────────────────────

    async def cancel_all_orders(self) -> None:
        """Cancel all open limit/stop orders and drop active brackets."""
        try:
            if self._product_id is not None:
                body = {
                    "product_id":     self._product_id,
                    "product_symbol": self._product_symbol,
                    "cancel_limit_orders": True,
                    "cancel_stop_orders":  True,
                }
                session = await self._http_session()
                await _signed_request(session, "DELETE", "/v2/orders", body)
                logger.debug("[OM] cancel_all_orders: done")
            else:
                logger.debug("[OM] cancel_all_orders: no product_id yet — skipping")
        except Exception as exc:
            exc_str = str(exc)
            if "bad_schema" in exc_str and "id" in exc_str:
                logger.debug(f"[OM] cancel_all_orders: no open orders on exchange (skipped)")
            else:
                logger.warning(f"[OM] cancel_all_orders failed (ignored): {exc}")
        
        await self.cancel_bracket()

    async def close_position(
        self,
        is_long: bool,
        reason: str = "Exit",
    ) -> dict:
        """Close position with reduce-only market order and sweep up safety bracket."""
        side = "sell" if is_long else "buy"
        logger.info(f"[OM] Closing position | side={side}  reason={reason}")
        try:
            order = await _retry(lambda: self.exchange.create_order(
                symbol = SYMBOL,
                type   = "market",
                side   = side,
                amount = ALERT_QTY,
                params = {"reduce_only": True},
            ))
            fill = float(order.get("average") or order.get("price") or 0.0)
            logger.info(f"[OM] Position closed | id={order.get('id')}  fill={fill:.2f}")
            
            # Strong reference background task tracking to bypass GC pruning
            task = asyncio.create_task(self.cancel_bracket())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            
            return order
        except ccxt.ExchangeError as exc:
            msg = str(exc).lower()
            if any(phrase in msg for phrase in _ALREADY_CLOSED_PHRASES):
                logger.info(f"[OM] close_position: position already gone. Returning sentinel.")
                return {"info": "already_closed"}
            raise

    # ── Price feed / Recovery metrics ──────────────────────────────────────────

    async def fetch_ticker(self) -> Optional[dict]:
        """Fetch current asset quote mark data."""
        try:
            ticker = await _retry(lambda: self.exchange.fetch_ticker(SYMBOL))
            return ticker
        except Exception as exc:
            logger.warning(f"[OM] fetch_ticker failed: {exc}")
            return None

    async def fetch_bracket_fill_price(self) -> Optional[float]:
        """Fetch exact executed price data from history if bracket triggered silently."""
        if self._product_symbol is None:
            return None

        session = await self._http_session()
        close_side = "sell" if (self._is_long is not False) else "buy"

        # Layer 1: Fill History Match
        try:
            fills_path = f"/v2/fills?product_symbol={self._product_symbol}&page_size=5"
            data = await _signed_request(session, "GET", fills_path)
            fills = (data.get("result") or [])
            if isinstance(fills, dict):
                fills = fills.get("data") or fills.get("fills") or []
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                if str(fill.get("side") or "").lower() == close_side:
                    price_raw = fill.get("fill_price") or fill.get("price") or fill.get("average")
                    if price_raw and float(price_raw) > 0:
                        return float(price_raw)
        except Exception as exc:
            logger.warning(f"[OM] fetch_bracket_fill_price layer-1 failed: {exc}")

        # Layer 2: Order State Audit Match
        try:
            hist_path = f"/v2/history/orders?product_symbol={self._product_symbol}&states=filled&page_size=10"
            data = await _signed_request(session, "GET", hist_path)
            orders = (data.get("result") or [])
            if isinstance(orders, dict):
                orders = orders.get("data") or orders.get("orders") or []
            for order in orders:
                if not isinstance(order, dict):
                    continue
                if str(order.get("side") or "").lower() == close_side and "fill" in str(order.get("state") or "").lower():
                    price_raw = order.get("average_fill_price") or order.get("average") or order.get("price")
                    if price_raw and float(price_raw) > 0:
                        return float(price_raw)
        except Exception as exc:
            logger.warning(f"[OM] fetch_bracket_fill_price layer-2 failed: {exc}")

        return None
