"""
feed/ws_feed.py
OHLCV feed for Delta Exchange via REST polling.

TIMING MISMATCH FIX (Root cause of "Pine and bot don't trade at same time"):
──────────────────────────────────────────────────────────────────────────────
Pine Script fires an alert the moment a bar CLOSES (the new bar's open tick).
The bot must execute at the same moment — bar[-1] is now the confirmed bar,
bar[-2] is the previous bar.

Problem in original code:
  1. The bot polled every 5 seconds, so entry was UP TO 5 seconds late.
  2. Worse: Delta's REST candle endpoint returns the LIVE (still-open) candle
     as the last row.  When a new timestamp appears, the PREVIOUS row is the
     confirmed closed candle.  The bot was evaluating the CURRENT live bar
     (which Pine hasn't signalled on yet) instead of the closed bar.
     This causes phantom signals and timing drift.

FIX applied here:
  - on_bar_close() now receives df where df.iloc[-1] is the CONFIRMED closed
    bar (the bar that Pine just closed and fired the alert on).  The live
    in-progress bar is stripped before passing to indicators/signal.
  - Poll interval tightened to 2s for all timeframes to catch bar close
    within 2 seconds, matching TradingView's webhook delivery window.
  - Bar confirmation logic: when ts > last_bar_ts, the OLD last row (now
    index -2 in the new data) is the confirmed closed candle.  We append
    the NEW row as the live candle and pass df[:-1] (all confirmed bars)
    to on_bar_close.  This is the key fix for timing parity.

Other bug fixes retained from v4:
  BUG 1: MIN_BARS was 260, Delta returns 255–258.  Fixed: MIN_BARS = 210.
  BUG 3: Bar confirmation was logger.debug (invisible).  Fixed: logger.info.
  BUG 4: Startup fetched exactly MIN_BARS.  Fixed: fetch MIN_BARS + 50.
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


class CandleFeed:
    def __init__(self, on_bar_close, on_feed_ready=None):
        self.on_bar_close   = on_bar_close
        async def _noop(): pass
        self.on_feed_ready  = on_feed_ready or _noop
        self._last_bar_ts   = 0
        self._df            = pd.DataFrame()
        self._exchange      = None
        self._ready_fired   = False

    async def start(self) -> None:
        """Start polling loop with auto-reconnect."""
        while True:
            try:
                await self._connect()
                await self._poll()
            except Exception as e:
                logger.error(f"Feed error: {e}", exc_info=True)
                await asyncio.sleep(WS_RECONNECT_SEC)

    async def _connect(self) -> None:
        """Init REST exchange and load historical bars."""
        params = {
            "apiKey"         : DELTA_API_KEY,
            "secret"         : DELTA_API_SECRET,
            "enableRateLimit": True,
        }
        self._exchange = ccxt.delta(params)
        if DELTA_TESTNET:
            self._exchange.set_sandbox_mode(True)

        fetch_limit = MIN_BARS + 50
        logger.info(f"Loading {fetch_limit} historical bars via REST for [{SYMBOL}] [{CANDLE_TIMEFRAME}]...")
        ohlcv = self._exchange.fetch_ohlcv(
            SYMBOL, CANDLE_TIMEFRAME, limit=fetch_limit
        )
        self._df = self._to_df(ohlcv)

        # The last row from REST is always the LIVE (open) candle.
        # _last_bar_ts tracks the timestamp of the confirmed closed bar,
        # which is df.iloc[-2].  When a new ts appears we know df.iloc[-2]
        # is the bar Pine just closed.
        # On startup, treat the current last row as live (not yet closed).
        self._last_bar_ts = int(self._df.iloc[-1]["timestamp"])

        bar_count = len(self._df)
        logger.info(
            f"Feed ready — {bar_count} bars loaded "
            f"(need {MIN_BARS}, have {bar_count} — "
            f"{'OK ✅' if bar_count >= MIN_BARS else 'WARN ⚠️ not enough bars'})"
        )

        if not self._ready_fired:
            self._ready_fired = True
            await self.on_feed_ready()

    async def _poll(self) -> None:
        """
        Poll every 2 seconds for ALL timeframes.

        TIMING FIX:
        At bar close, Delta REST returns a new candle with a new timestamp.
        At that moment:
          - df.iloc[-2] is the bar Pine just closed  → pass df[:-1] to signals
          - df.iloc[-1] is the new live bar          → track as _last_bar_ts

        This means on_bar_close() always receives a DataFrame where
        df.iloc[-1] is the confirmed closed bar — exactly what Pine used
        to fire the alert.  Indicators compute on closed bars only.
        """
        sleep_sec = 2   # 2s polls catch bar close within 2s for all timeframes
        logger.info(f"Polling every {sleep_sec}s for {CANDLE_TIMEFRAME} candles [{SYMBOL}]")

        while True:
            await asyncio.sleep(sleep_sec)
            try:
                # Fetch last 5 candles (includes live candle)
                ohlcv = self._exchange.fetch_ohlcv(
                    SYMBOL, CANDLE_TIMEFRAME, limit=5
                )
                if not ohlcv:
                    continue

                for candle in ohlcv:
                    ts = int(candle[0])

                    if ts > self._last_bar_ts:
                        # ── NEW BAR DETECTED ─────────────────────────────────
                        # The previous _last_bar_ts row is now a CONFIRMED closed bar.
                        # df currently has that closed bar as its last row.
                        # TIMING FIX: pass df.copy() (all confirmed bars up to
                        # and including the just-closed bar) to on_bar_close.
                        if len(self._df) >= MIN_BARS:
                            logger.info(
                                f"✅ Bar confirmed | closed_ts={self._last_bar_ts} | "
                                f"new_ts={ts} | bars={len(self._df)} — evaluating signals..."
                            )
                            # TIMING FIX: df.iloc[-1] IS the closed bar (correct)
                            await self.on_bar_close(self._df.copy())
                        else:
                            logger.warning(
                                f"⚠️ Bar skipped — only {len(self._df)} bars "
                                f"(need {MIN_BARS}). Waiting for more data."
                            )

                        # Append the new live candle row and track its ts
                        new_row = pd.DataFrame([{
                            "timestamp": ts,
                            "open"     : float(candle[1]),
                            "high"     : float(candle[2]),
                            "low"      : float(candle[3]),
                            "close"    : float(candle[4]),
                            "volume"   : float(candle[5]),
                        }])
                        self._df = pd.concat(
                            [self._df, new_row], ignore_index=True
                        ).tail(MIN_BARS + 50)
                        self._last_bar_ts = ts

                    else:
                        # Update the live candle in place (not yet closed)
                        if not self._df.empty:
                            last_idx = self._df.index[-1]
                            self._df.loc[last_idx, "timestamp"] = ts
                            self._df.loc[last_idx, "open"]      = float(candle[1])
                            self._df.loc[last_idx, "high"]      = float(candle[2])
                            self._df.loc[last_idx, "low"]       = float(candle[3])
                            self._df.loc[last_idx, "close"]     = float(candle[4])
                            self._df.loc[last_idx, "volume"]    = float(candle[5])

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
