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

FIX-BRACKET-ID (2026-05-13):
─────────────────────────────────────────────────────────────────────────────
Delta India's POST /v2/orders/bracket returns the SL/TP order IDs nested
inside the response (either as a list under `result` or under
`result.stop_loss_order.id`) — NOT at `result.id` as v10's original code
assumed. When parsing failed, `_bracket_order_id` stayed None and every
subsequent `update_bracket_sl()` was rejected with the warning
"no bracket_order_id, cannot update", leaving the trail SL on Python-side
only (defeating the whole point of Phase-2). This file now:
  1. Probes multiple response shapes to find the SL order id.
  2. Falls back to a `fetch_open_orders()` discovery pass if parsing fails.
  3. Marks the bracket as active either way (the bracket exists on Delta;
     we just need its id to mutate it).

How it plugs in
─────────────────────────────────────────────────────────────────────────────
The public API of OrderManager is UNCHANGED. main.py and trail_loop.py
keep calling place_entry / cancel_all_orders / close_position exactly as
before. The bracket flow is handled internally:

  1. place_entry(is_long, sl, tp)
     • Send market entry (as before).
     • Wait for fill.
     • IMMEDIATELY POST /v2/orders/bracket attaching SL + TP to the
       open position. Save the bracket order id.

  2. update_bracket_sl(new_sl)        ← PUBLIC METHOD
     • Called by TrailMonitor when the trail tightens or BE activates.
     • PUT /v2/orders/bracket — Delta updates the existing bracket SL
       in place. No race, no cancel-and-replace.

  3. close_position(is_long, reason)
     • Used only as a safety net for cases the bracket can't handle:
       Max SL (uses live ATR, not entry ATR — Pine logic), manual
       intervention, etc. If the bracket fired first, this returns
       {"info": "already_closed"} as before — no behavioral change.

  4. cancel_bracket()
     • Removes the SL + TP from Delta (called on shutdown / stop).

Endpoints used
─────────────────────────────────────────────────────────────────────────────
  POST  /v2/orders/bracket   place SL + TP on existing position
  PUT   /v2/orders/bracket   update SL / TP / trail of existing bracket
  DELETE /v2/orders/bracket  remove the bracket (used in cancel_bracket)
  POST  /v2/orders           market entry (unchanged)

All four are signed with HMAC-SHA256 over (METHOD + TIMESTAMP + PATH + BODY)
per Delta India's auth spec — same scheme ccxt already uses internally.
We use the raw signed-request path for the bracket endpoints because ccxt
does not expose them in its high-level API yet.

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
import time
from typing import Any, Optional

import aiohttp
import ccxt.async_support as ccxt

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY,
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


# ─── Delta India signed REST helper (for bracket endpoints) ───────────────────
#
# ccxt does not expose Delta's bracket endpoints, so we sign requests manually.
# Auth scheme (per Delta docs):
#   signature_data = METHOD + TIMESTAMP + PATH_WITH_QUERY + JSON_BODY
#   signature      = HMAC_SHA256(api_secret, signature_data).hexdigest()
# Headers:
#   api-key, signature, timestamp, Content-Type: application/json

def _sign(method: str, ts: str, path: str, body: str) -> str:
    msg = (method + ts + path + body).encode()
    return hmac.new(DELTA_API_SECRET.encode(), msg, hashlib.sha256).hexdigest()


async def _signed_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    body_obj: Optional[dict] = None,
) -> dict:
    """
    Make a signed HTTP request to Delta India for endpoints not in ccxt.
    Returns the parsed JSON response. Raises on HTTP / parse errors.
    """
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


# ─── ID extraction helper (FIX-BRACKET-ID) ─────────────────────────────────────

def _extract_sl_order_id(bracket_resp: dict) -> Optional[int]:
    """
    Pull the stop-loss order id out of Delta India's POST /v2/orders/bracket
    response. Delta's response shape has varied across API revisions; rather
    than locking to one, try every shape we've seen in the wild:

      A) result: [ {"id": 1, "stop_order_type": "stop_loss_order"}, ... ]
      B) result: { "stop_loss_order": {"id": 1, ...},
                   "take_profit_order": {"id": 2, ...} }
      C) result: { "id": 1, "stop_order_type": "stop_loss_order", ... }
      D) result: { "id": 1 }      (single id; assume SL)

    Returns the int id, or None if nothing matches.
    """
    result = bracket_resp.get("result")
    if result is None:
        return None

    def _coerce(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Shape A — list of orders
    if isinstance(result, list):
        # First, try to find one explicitly tagged as a stop-loss.
        for item in result:
            if not isinstance(item, dict):
                continue
            stype = str(
                item.get("stop_order_type")
                or item.get("order_type")
                or ""
            ).lower()
            if "stop_loss" in stype or stype == "stop_loss_order":
                rid = _coerce(item.get("id"))
                if rid is not None:
                    return rid
        # Fallback: just take the first id we can find.
        for item in result:
            if isinstance(item, dict):
                rid = _coerce(item.get("id"))
                if rid is not None:
                    return rid
        return None

    # Shapes B / C / D — dict
    if isinstance(result, dict):
        # B: nested stop_loss_order block
        sl_block = result.get("stop_loss_order")
        if isinstance(sl_block, dict):
            rid = _coerce(sl_block.get("id"))
            if rid is not None:
                return rid

        # C: dict with explicit stop_order_type
        stype = str(
            result.get("stop_order_type")
            or result.get("order_type")
            or ""
        ).lower()
        if "stop_loss" in stype:
            rid = _coerce(result.get("id"))
            if rid is not None:
                return rid

        # D: bare id
        rid = _coerce(result.get("id"))
        if rid is not None:
            return rid

    return None


# ─── OrderManager ─────────────────────────────────────────────────────────────

class OrderManager:
    """
    Async Delta Exchange order manager with Phase-2 bracket-order support.

    Instantiated once in main.py's ShivaSniperBot and shared with TrailMonitor.
    """

    def __init__(self) -> None:
        self.exchange: ccxt.delta = build_exchange()

        # PHASE-2 state — set on entry fill, cleared on exit.
        self._product_id:    Optional[int]   = None  # numeric Delta id of SYMBOL
        self._product_symbol: Optional[str]  = None  # raw Delta symbol (e.g. "BTCUSD")
        self._bracket_order_id: Optional[int] = None  # ID of the active bracket SL order
        self._bracket_active:        bool    = False
        self._current_sl:    Optional[float] = None
        self._current_tp:    Optional[float] = None
        self._is_long:       Optional[bool]  = None  # cached for bracket math

        # Reusable HTTP session for the signed-bracket endpoints. Lazily created.
        self._http: Optional[aiohttp.ClientSession] = None

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

        # PHASE-2: resolve numeric product_id and raw Delta symbol once.
        market = self.exchange.markets[SYMBOL]
        info   = market.get("info") or {}
        # Delta returns the numeric id under "id" or "product_id"; fall back
        # to ccxt's market id field. All three are the same value.
        pid    = info.get("id") or info.get("product_id") or market.get("id")
        psym   = info.get("symbol") or market.get("baseId", "") + market.get("quoteId", "")
        try:
            self._product_id = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            self._product_id = None
        # ccxt's id for Delta perps is the numeric product id as a string;
        # the raw symbol (e.g. "BTCUSD") lives in info.symbol.
        self._product_symbol = info.get("symbol") or "BTCUSD"

        if self._product_id is None:
            logger.warning(
                f"[OM] Could not resolve numeric product_id for {SYMBOL}; "
                f"bracket orders will be DISABLED for this run. "
                f"Bot will fall back to Python-side SL management."
            )
        else:
            logger.info(
                f"[OM] Resolved product_id={self._product_id} "
                f"product_symbol={self._product_symbol}"
            )

        logger.info(f"[OM] Initialized — symbol={SYMBOL}  qty={ALERT_QTY}")

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
            self._http = aiohttp.ClientSession()
        return self._http

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

    # Backward-compat alias — older modules (phase3, execution.py, and any stale
    # VPS code) call this name. Both names return the same data. This prevents
    # the "'OrderManager' object has no attribute 'fetch_position'" AttributeError
    # from crashing the bar handler mid-trade.
    async def fetch_position(self) -> Optional[dict]:
        return await self.fetch_open_position()

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_entry(
        self,
        is_long: bool,
        sl: float,
        tp: float,
    ) -> dict:
        """
        Place a market entry order, then attach an EMERGENCY-ONLY bracket
        SL at the initial SL level.

        The bracket is a crash/disconnect safety net placed ONCE and NEVER
        amended. Python (TrailMonitor) owns all trail/BE/exit logic and
        fires exits via close_position(). The bracket only fires if the bot
        loses connectivity or crashes while the position is open.

        tp is accepted for signature compatibility but is NOT sent to Delta
        — Python handles TP detection. Sending TP to the bracket would race
        against the Python exit path and cause double-close errors.

        Returns the ccxt order dict for the entry leg. Raises on entry
        failure. If bracket attach fails the trade is still open and
        TrailMonitor protects it — does not raise.
        """
        side = "buy" if is_long else "sell"
        logger.info(
            f"[OM] Placing entry | side={side}  qty={ALERT_QTY}  "
            f"sl={sl:.2f}  tp={tp:.2f}"
        )

        # ── 1. Market entry ──────────────────────────────────────────────────
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

        # ── 2. Cache state ───────────────────────────────────────────────────
        self._is_long          = is_long
        self._current_sl       = float(sl)
        self._current_tp       = float(tp)
        self._bracket_active   = False
        self._bracket_order_id = None

        # ── 3. Emergency bracket SL (placed once, never amended) ─────────────
        if self._product_id is None:
            logger.warning(
                "[OM] Emergency bracket disabled (no product_id). "
                "TrailMonitor is sole protection."
            )
            return order

        try:
            bracket_resp = await self._place_bracket(sl=sl)
            self._bracket_active = True
            logger.info(
                f"[OM] ✅ Emergency bracket SL placed on Delta | "
                f"sl={sl:.2f}  (never amended — Python trail owns exits)"
            )
        except Exception as exc:
            logger.error(
                f"[OM] ⚠️  Emergency bracket FAILED — trade is open with no "
                f"exchange-side safety net. TrailMonitor is sole protection. "
                f"Error: {exc}"
            )

        return order

    # ── Bracket management ─────────────────────────────────────────────────────

    async def _place_bracket(self, sl: float) -> dict:
        """
        POST /v2/orders/bracket — emergency SL only, no TP.

        Placed ONCE after entry and NEVER amended. Python (TrailMonitor)
        fires all real exits. This bracket only fires if the bot crashes
        or loses connectivity.
        """
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

    # NOTE: _discover_bracket_sl_id and update_bracket_sl are intentionally
    # removed. The bracket is never amended so there is nothing to discover
    # or update. TrailMonitor._push_sl_to_delta() is also removed.
    # See FIX-BRACKET-CHURN in the module docstring.


    async def cancel_bracket(self) -> None:
        """
        DELETE /v2/orders/bracket — remove the SL + TP from Delta.
        Called on shutdown and on any exit path where the bracket needs
        to be cleaned up before a manual close. Never raises.
        """
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
            self._bracket_order_id = None
            self._current_sl       = None
            self._current_tp       = None
            self._is_long          = None

    # ── Order management ──────────────────────────────────────────────────────

    async def cancel_all_orders(self) -> None:
        """
        Cancel all open orders for the symbol (and the bracket).
        Never raises — failures are logged and swallowed (best-effort cleanup).

        FIX-CANCEL-01: replaced ccxt.cancel_all_orders() with a direct Delta
        REST DELETE /v2/orders call. The ccxt async version internally called
        Exchange.request() without awaiting it, producing a RuntimeWarning
        every time this method ran. The signed REST path is already used
        throughout this file for bracket operations and is reliable.
        """
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
            # FIX-CANCEL-RECOVERY: Delta returns 400 bad_schema "id required" when
            # there are no open orders to cancel (common on recovery restart where
            # the previous session's bracket is already gone). Downgrade to DEBUG.
            if "bad_schema" in exc_str and "id" in exc_str:
                logger.debug(f"[OM] cancel_all_orders: no open orders on exchange (skipped)")
            else:
                logger.warning(f"[OM] cancel_all_orders failed (ignored): {exc}")
        # PHASE-2: also drop the bracket
        await self.cancel_bracket()

    async def close_position(
        self,
        is_long: bool,
        reason: str = "Exit",
    ) -> dict:
        """
        Close the open position with a reduce-only market order.

        PHASE-2 BEHAVIOR:
        ──────────────────────────────────────────────────────────────────
        With Delta-side brackets active, most exits happen at the matching
        engine — by the time TrailMonitor calls close_position, the bracket
        has usually already filled. The exchange will then return
        "no_position_for_reduce_only" which we map to {"info":"already_closed"}
        — same sentinel as Phase-1, no behavioral change for the caller.

        close_position is still the right path for:
          • Max SL — uses live ATR (Pine logic), not the static bracket SL.
          • Manual /stop command from Telegram.
          • Recovery cleanup if state is inconsistent.

        Before sending the close, we cancel the bracket so we don't end up
        with an orphan SL/TP order on Delta after the position goes flat.

        FIX-BRACKET-DELAY: bracket cancel now runs AFTER the market close order
        fires (in background). Previously cancel_bracket() ran first — when the
        bracket was already gone (404) this wasted ~1 second during which price
        moved against the exit. Market close now fires immediately, recovering
        ~58pts of slippage per trade.
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
            # FIX-BRACKET-DELAY: cancel bracket AFTER fill — background task,
            # never blocks the exit path.
            asyncio.get_event_loop().create_task(self.cancel_bracket())
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
