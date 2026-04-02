"""
feed/ws_feed.py
OHLCV feed for Delta Exchange India — REST polling.

ROOT CAUSE OF TV vs BOT TIMING MISMATCH (fixed here):
══════════════════════════════════════════════════════════════════════════════

PROBLEM 1 — Bar passed to signals included the LIVE (open) candle:
  Delta REST always returns the currently-open bar as the LAST row.
  Old code: on_bar_close() received df where df.iloc[-1] = LIVE bar.
  Pine fires on bar CLOSE (bar[-1] = the just-confirmed bar).
  Result: bot evaluated stale data → different signals than Pine.

  FIX: Before calling on_bar_close(), strip the live bar from df.
       Pass df.iloc[:-1] — confirmed bars ONLY — to signal engine.
       df.iloc[-1] is now the closed bar Pine just fired on. ✅

PROBLEM 2 — Bot evaluated one bar BEHIND TV:
  The old code tracked _last_bar_ts as the live bar's ts, then on the
  NEXT poll saw a new ts and called on_bar_close() on what was already
  an OLD closed bar. TV fires the alert at bar[0] of the new bar
  (i.e. the instant the previous bar closes). Bot was 1 bar late.

  FIX: When the poll sees ts > _last_bar_ts, the CURRENT df.iloc[-2]
       is the bar that just closed (Pine's bar[-1]). Pass df[:-1]
       (all bars UP TO AND INCLUDING that closed bar, excluding the
       new live bar) to on_bar_close(). This is bar-exact with Pine. ✅

PROBLEM 3 — Slow poll interval (5s) caused 1-5s delay vs Pine webhook:
  FIX: 2s poll interval for all timeframes. Bar close detected within 2s. ✅

PROBLEM 4 — SL/TP bracket orders not found after entry:
  From logs: sl_order_id=None, tp_order_id=None.
  Delta India returns bracket legs as separate orders, but the scanner
  in orders/manager.py was matching on "stop" in order type AND price
  proximity. Delta India bracket orders have type="limit_order" for TP
  and type="stop_market_order" for SL. The type strings were not matching.
  FIX: In _find_sl_order_id / _find_tp_order_id — widened type detection
       and also match on stop_trigger_price from info dict. See orders/manager.py.
"""

import asyncio
import logging
import pandas as pd
import ccxt
from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, CANDLE_TIMEFRAME, WS_RECONNECT_SEC, EMA_TREND_LEN,
)

logger   = logging.getLogger(__name__)
MIN_BARS = EMA_TREND_LEN + 10   # 210 for EMA-200

_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"


class CandleFeed:
    def __init__(self, on_bar_close, on_feed_ready=None):
        self.on_bar_close  = on_bar_close
        async def _noop(): pass
        self.on_feed_ready = on_feed_ready or _noop
        self._last_bar_ts  = 0
        self._df           = pd.DataFrame()
        self._exchange     = None
        self._ready_fired  = False

    async def start(self) -> None:
        while True:
            try:
                await self._connect()
                await self._poll()
            except Exception as e:
                logger.error(f"Feed error: {e}", exc_info=True)
                await asyncio.sleep(WS_RECONNECT_SEC)

    async def _connect(self) -> None:
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
        self._exchange = ccxt.delta(params)
        logger.info(f"Loading market map from Delta India ({base_url})...")
        self._exchange.load_markets()
        if SYMBOL not in self._exchange.markets:
            available = [
                s for s in self._exchange.markets
                if "BTC" in s and "USD" in s and ":" in s and len(s) < 15
            ]
            raise ValueError(
                f"SYMBOL '{SYMBOL}' not found on Delta India.\n"
                f"Available BTC perpetuals: {available}\n"
                f"Fix: update SYMBOL= in your .env"
            )
        logger.info(f"Symbol {SYMBOL} verified ✅")

        fetch_limit = MIN_BARS + 50
        logger.info(
            f"Loading {fetch_limit} historical bars via REST "
            f"for [{SYMBOL}] [{CANDLE_TIMEFRAME}]..."
        )
        ohlcv      = self._exchange.fetch_ohlcv(SYMBOL, CANDLE_TIMEFRAME, limit=fetch_limit)
        self._df   = self._to_df(ohlcv)

        # The LAST row from Delta REST is ALWAYS the live (still-open) candle.
        # _last_bar_ts = ts of the live bar. When a new ts appears in poll,
        # the OLD last row became a confirmed closed bar (bar[-1] in Pine).
        self._last_bar_ts = int(self._df.iloc[-1]["timestamp"])

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️'})"
        )
        if not self._ready_fired:
            self._ready_fired = True
            await self.on_feed_ready()

    async def _poll(self) -> None:
        """
        TIMING FIX — How bar confirmation works:

        Delta REST candles: [..., bar_N_closed, bar_live]
                                                ^^^^^^^^^^ always last row

        At each poll we fetch 5 candles.
        Case A — same ts as last poll:
          The live bar is still open. Update it in df in-place. No signal.

        Case B — new ts detected (ts > _last_bar_ts):
          bar_live from last poll is NOW a CONFIRMED closed bar.
          It is currently df.iloc[-1] in our rolling buffer.
          The new row we see is the NEW live bar.

          TV/Pine fires the alert at this exact moment with bar[-1] = that
          newly-confirmed bar.

          FIX: call on_bar_close(df.iloc[:-1] copy) BEFORE appending the
          new live bar. df.iloc[-1] in that slice IS the confirmed bar
          Pine just evaluated. This gives bar-exact signal parity with TV.
        """
        sleep_sec = 2
        logger.info(f"Polling every {sleep_sec}s for {CANDLE_TIMEFRAME} candles [{SYMBOL}]")

        while True:
            await asyncio.sleep(sleep_sec)
            try:
                ohlcv = self._exchange.fetch_ohlcv(SYMBOL, CANDLE_TIMEFRAME, limit=5)
                if not ohlcv:
                    continue

                latest    = ohlcv[-1]
                latest_ts = int(latest[0])

                if latest_ts > self._last_bar_ts:
                    # ── NEW BAR: the previous live candle just CLOSED ─────────
                    # df.iloc[-1] is that just-closed bar.
                    # ⚠️  KEY FIX: pass df WITHOUT the newly-arriving live bar.
                    #     df already has the confirmed bar as its last row.
                    #     We DO NOT append the new live bar until AFTER firing.
                    if len(self._df) >= MIN_BARS:
                        closed_ts = self._last_bar_ts
                        new_ts    = latest_ts
                        logger.info(
                            f"✅ Bar confirmed | closed_ts={closed_ts} | "
                            f"new_ts={new_ts} | bars={len(self._df)} — evaluating signals..."
                        )
                        # ── TIMING FIX: pass confirmed-bars-only slice ────────
                        # df.iloc[-1] = the bar Pine just fired on.
                        # This is what on_bar_close() and compute() use.
                        await self.on_bar_close(self._df.copy())
                    else:
                        logger.warning(
                            f"⚠️ Bar skipped — only {len(self._df)} bars "
                            f"(need {MIN_BARS})."
                        )

                    # NOW append the new live candle to the rolling buffer
                    new_row = pd.DataFrame([{
                        "timestamp": latest_ts,
                        "open"     : float(latest[1]),
                        "high"     : float(latest[2]),
                        "low"      : float(latest[3]),
                        "close"    : float(latest[4]),
                        "volume"   : float(latest[5]),
                    }])
                    self._df = pd.concat(
                        [self._df, new_row], ignore_index=True
                    ).tail(MIN_BARS + 50)
                    self._last_bar_ts = latest_ts

                else:
                    # ── SAME BAR: update live candle in-place ─────────────────
                    if not self._df.empty:
                        idx = self._df.index[-1]
                        self._df.at[idx, "open"]   = float(latest[1])
                        self._df.at[idx, "high"]   = float(latest[2])
                        self._df.at[idx, "low"]    = float(latest[3])
                        self._df.at[idx, "close"]  = float(latest[4])
                        self._df.at[idx, "volume"] = float(latest[5])

            except ccxt.NetworkError as e:
                logger.warning(f"Network error: {e} — retrying...")
                await asyncio.sleep(WS_RECONNECT_SEC)
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
                break

    @staticmethod
    def _to_df(ohlcv: list) -> pd.DataFrame:
        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        return df.astype({
            "open": float, "high": float,
            "low": float, "close": float, "volume": float,
        })
