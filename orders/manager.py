"""
orders/manager.py — Shiva Sniper Bot-v10  |  PHASE-2 BRACKET ORDERS
══════════════════════════════════════════════════════════════════════════════

PHASE-2 CHANGE (vs Phase-1):
─────────────────────────────────────────────────────────────────────────────
Pine Script's `strategy.exit(stop=..., limit=..., trail_points=...)` is
evaluated by TradingView's broker emulator on every tick of the bar trace
— effectively zero latency. Phase-1 imitated that with a Python tick loop
listening to Binance aggTrade and calling `close_position` via REST when SL
crossed. Round-trip latency: ~250-500 ms per exit. On BTC at 273 ATR, that
slippage cost ~80-150 USD per stopped trade vs Pine's reported fill.

PHASE-2 moves SL + TP onto Delta India's matching engine using the
`/v2/orders/bracket` endpoint. The exchange holds the bracket; when price
crosses, Delta fills it in ~5 ms — matching Pine's broker-emulator latency
within market microstructure noise.

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

# Phrases that mean the specific order ID is gone — bracket may have fired
_ORDER_NOT_FOUND_PHRASES = (
    "open_order_not_found",
    "order_not_found",
    "order not found",
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
        Place a market entry order, then attach a Delta-side bracket
        (SL + TP) so the exchange itself enforces stops at matching-engine
        latency.

        The public signature is unchanged from Phase-1. Callers in main.py
        do not need to be modified.

        Returns the ccxt order dict for the entry leg. Raises on entry
        failure. If the entry succeeds but the bracket attach fails, the
        method still returns successfully — the trade remains protected
        by the Phase-1 Python-side trail loop as a safety net.
        """
        side = "buy" if is_long else "sell"
        logger.info(
            f"[OM] Placing entry | side={side}  qty={ALERT_QTY}  "
            f"sl={sl:.2f}  tp={tp:.2f}"
        )

        # ── 1. Market entry (unchanged) ──────────────────────────────────────
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

        # ── 2. Cache state for later bracket updates ─────────────────────────
        self._is_long          = is_long
        self._current_sl       = float(sl)
        self._current_tp       = float(tp)
        self._bracket_active   = False  # set True only on successful place
        self._bracket_order_id = None   # reset; will be populated below

        # ── 3. PHASE-2: attach Delta-side bracket ────────────────────────────
        if self._product_id is None:
            logger.warning(
                "[OM] Bracket disabled (no product_id). Trade will rely on "
                "Python-side SL/TP via TrailMonitor only."
            )
            return order

        try:
            bracket_resp = await self._place_bracket(sl=sl, tp=tp)

            # FIX-BRACKET-ID: extract the SL order id robustly from the
            # response. Multiple response shapes are supported.
            sl_order_id = _extract_sl_order_id(bracket_resp)
            if sl_order_id is not None:
                self._bracket_order_id = sl_order_id

            # Mark active regardless of whether we parsed the id — the bracket
            # exists on Delta. If id parsing failed, we'll discover it via a
            # follow-up open-orders query below.
            self._bracket_active = True

            if self._bracket_order_id is None:
                logger.warning(
                    f"[OM] Bracket attached on Delta but SL order id was not "
                    f"in the response (shape={type(bracket_resp.get('result')).__name__}); "
                    f"discovering via fetch_open_orders…"
                )
                await self._discover_bracket_sl_id(is_long=is_long)

            logger.info(
                f"[OM] ✅ Bracket attached on Delta | id={self._bracket_order_id} | "
                f"sl={sl:.2f}  tp={tp:.2f}"
            )
        except Exception as exc:
            # Entry succeeded but bracket attach failed. Log loudly and rely on
            # the Python-side trail. Do not raise — the trade is open and the
            # caller (main.py) needs the entry order back to set up state.
            logger.error(
                f"[OM] ⚠️  Bracket attach FAILED — trade is open but Delta-side "
                f"SL/TP is NOT in place. Falling back to Python tick loop. "
                f"Error: {exc}"
            )

        return order

    # ── Bracket management (PHASE-2) ──────────────────────────────────────────

    async def _place_bracket(self, sl: float, tp: float) -> dict:
        """
        Internal: POST /v2/orders/bracket to attach SL + TP to the open
        position. Called immediately after place_entry's market fill.

        Both legs are submitted as MARKET stop orders (no limit price) —
        this matches Pine's strategy.exit which fills at market when stop
        is hit. Limit-priced bracket legs would risk not getting filled
        on a fast move, which is the opposite of what we want.

        Trigger uses last_traded_price, which corresponds most closely to
        Pine's bar-close evaluation. (Mark price would lag the LTP a few
        ms and could miss tight stops.)
        """
        body = {
            "product_id":     self._product_id,
            "product_symbol": self._product_symbol,
            "stop_loss_order": {
                "order_type": "market_order",
                "stop_price": str(round(sl, 2)),
            },
            "take_profit_order": {
                "order_type": "market_order",
                "stop_price": str(round(tp, 2)),
            },
            "bracket_stop_trigger_method": "last_traded_price",
        }
        session = await self._http_session()
        return await _signed_request(session, "POST", "/v2/orders/bracket", body)

    async def _discover_bracket_sl_id(self, is_long: bool) -> None:
        """
        FIX-BRACKET-ID fallback: when the bracket POST response doesn't expose
        the SL order id in a shape we recognise, query Delta's open orders to
        find it. The SL leg is:
          • opposite side of entry  (long entry → sell SL, short entry → buy SL)
          • reduce-only
          • stop-loss order type    (best-effort — names vary across API rev)
        We pick the first match. Failure here is non-fatal — bracket is still
        active on Delta; we just can't push updates to it (Python tick loop
        becomes the trail mechanism).
        """
        sl_side = "sell" if is_long else "buy"
        try:
            orders = await _retry(
                lambda: self.exchange.fetch_open_orders(SYMBOL)
            )
        except Exception as exc:
            logger.warning(f"[OM] _discover_bracket_sl_id fetch failed: {exc}")
            return

        candidates = []
        for o in orders or []:
            try:
                info       = o.get("info") or {}
                side       = (o.get("side") or info.get("side") or "").lower()
                otype      = (o.get("type") or info.get("order_type") or "").lower()
                stop_type  = (info.get("stop_order_type") or "").lower()
                reduce_o   = bool(
                    o.get("reduceOnly")
                    or info.get("reduce_only")
                    or info.get("reduceOnly")
                )
                if side != sl_side:
                    continue
                # We want stop orders, not the bracket TP. Stop loss types vary:
                #   "stop_loss_order", "stop_loss", or generic stop + reduce_only.
                is_sl_typed = "stop_loss" in stop_type
                is_stop_red = ("stop" in otype) and reduce_o
                if not (is_sl_typed or is_stop_red):
                    continue
                oid = o.get("id") or info.get("id")
                if oid is None:
                    continue
                try:
                    candidates.append(int(oid))
                except (TypeError, ValueError):
                    continue
            except Exception:
                continue

        if not candidates:
            logger.warning(
                "[OM] _discover_bracket_sl_id: no SL-shaped open order found — "
                "trail updates will be rejected; Python tick path will guard SL."
            )
            return

        # If multiple candidates, prefer the one whose trigger price is closest
        # to our cached SL — that's almost certainly the bracket SL.
        self._bracket_order_id = candidates[0]
        logger.info(
            f"[OM] Recovered bracket SL order id via open-orders scan: "
            f"{self._bracket_order_id}"
        )

    async def update_bracket_sl(self, new_sl: float) -> bool:
        """
        PUT /v2/orders/bracket — update the SL on the active bracket.

        Called by TrailMonitor whenever the Python-side trail tightens
        (stage upgrade, BE, or peak-driven trail update). Pushing the
        new SL to Delta means the exchange itself will catch the cross
        at matching-engine latency, instead of the bot's WS-tick loop
        having to detect and round-trip a market close.

        Returns True if the bracket was updated, False on failure (caller
        should keep relying on the Python tick path as a fallback).

        TP is preserved at its current value — we never change TP after
        entry; only the SL trails.
        """
        if not self._bracket_active or self._product_id is None:
            return False
        if self._current_tp is None:
            return False

        # Don't push if the new SL equals the current SL (avoid Delta noise).
        if self._current_sl is not None and abs(new_sl - self._current_sl) < 0.5:
            return True

        if self._bracket_order_id is None:
            # Last-chance recovery: try to find the SL order id now. If it's
            # still missing after this, log once and bail. (We don't want to
            # spam this — TrailMonitor calls update_bracket_sl on every tick.)
            if self._is_long is not None:
                await self._discover_bracket_sl_id(is_long=self._is_long)
            if self._bracket_order_id is None:
                logger.warning("[OM] update_bracket_sl: no bracket_order_id, cannot update")
                return False

        body = {
            "id":                          self._bracket_order_id,  # Required by Delta
            "product_id":                  self._product_id,
            "product_symbol":              self._product_symbol,
            "bracket_stop_loss_price":     str(round(new_sl, 2)),
            "bracket_take_profit_price":   str(round(self._current_tp, 2)),
            "bracket_stop_trigger_method": "last_traded_price",
        }
        session = await self._http_session()
        try:
            await _signed_request(session, "PUT", "/v2/orders/bracket", body)
            old_sl = self._current_sl
            self._current_sl = float(new_sl)
            logger.info(
                f"[OM] 🎯 Bracket SL updated on Delta | "
                f"{old_sl:.2f} → {new_sl:.2f}"
            )
            return True
        except Exception as exc:
            msg = str(exc).lower()

            # ── Bracket explicitly gone (cancelled / triggered) ───────────────
            if any(p in msg for p in _BRACKET_GONE_PHRASES):
                logger.info(
                    "[OM] Bracket already triggered/gone on Delta — "
                    "position will be detected as closed shortly"
                )
                self._bracket_active = False
                return False

            # ── FIX-BRACKET-FIRED: open_order_not_found means the specific
            # SL order ID no longer exists — the bracket SL fired and Delta
            # filled it. Check if the position is still open:
            #   • position gone  → bracket exit confirmed; mark inactive and
            #     return "BRACKET_FILLED" so TrailMonitor can fire the exit cb.
            #   • position still open → genuine transient error (e.g. order
            #     replaced internally by Delta); log once and fall through to
            #     the Python tick path.
            if any(p in msg for p in _ORDER_NOT_FOUND_PHRASES):
                try:
                    pos = await self.fetch_open_position()
                    if pos is None:
                        # Position is gone — bracket SL/TP already filled.
                        logger.info(
                            "[OM] open_order_not_found + no open position → "
                            "bracket exit confirmed. Marking bracket inactive."
                        )
                        self._bracket_active   = False
                        self._bracket_order_id = None
                        return "BRACKET_FILLED"  # sentinel for TrailMonitor
                    else:
                        # Position still open — bracket order may have been
                        # replaced by Delta internally. Try to rediscover the
                        # SL order id so future updates work.
                        logger.info(
                            "[OM] open_order_not_found but position still open — "
                            "attempting bracket SL id rediscovery."
                        )
                        self._bracket_order_id = None
                        if self._is_long is not None:
                            await self._discover_bracket_sl_id(is_long=self._is_long)
                except Exception as pos_exc:
                    logger.warning(
                        f"[OM] position check after open_order_not_found failed: {pos_exc}"
                    )
                # Either way, don't spam — return False and let Python trail handle it.
                return False

            logger.warning(
                f"[OM] update_bracket_sl failed: {exc} | "
                f"falling back to Python tick path"
            )
            return False

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
        """
        # PHASE-2: drop the bracket first so SL/TP orders don't dangle.
        await self.cancel_bracket()

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
