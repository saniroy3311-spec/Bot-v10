"""
main.py — Shiva Sniper Bot v10  (Live Runner)
══════════════════════════════════════════════════════════════════════════════

Entry point launched by systemd / PM2 / Docker CMD.

WHAT THIS FILE DOES
───────────────────
  1. Starts CandleFeed (WS primary, REST fallback).
  2. On every confirmed bar close → compute indicators → evaluate Pine
     entry conditions → enter or update trail.
  3. TrailMonitor handles all exits (TP, Trail SL, BE, Max SL) at tick
     resolution via the WS price push path.
  4. Sends Telegram notifications for entry and exit events.
  5. Persists trade records to SQLite (Journal).
  6. On restart mid-trade: detects existing position via fetch_open_position()
     and resumes trail management from the next bar close.

PINE PARITY
───────────
  Entry  : calc_on_every_tick=false → entry fires ONLY at confirmed bar close.
  Exit   : BinancePriceFeed pushes Binance aggTrade prices (~10ms) to
           TrailMonitor.on_price_tick() — same source as Pine's broker
           emulator. Stage upgrades + BE only at bar close (30m).
  Volume : FILTER_VOL_ENABLED=false by default — Delta REST volumes (~3% of
           TradingView's) are incomparable data sources. ATR + body filters
           still guard against dead/choppy bars.

RUNNING
───────
  python main.py
  systemctl start shiva_sniper
  docker run shiva_sniper_bot
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Optional

# ── Canonical module imports ───────────────────────────────────────────────────
from config import (
    TELEGRAM_ENABLED,
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME, FILTER_VOL_ENABLED,
    POSITION_BTC_SIZE, TREND_ATR_MULT, RANGE_ATR_MULT,
    ALLOW_REVERSAL,
)
from feed.ws_feed            import CandleFeed
from feed.binance_price_feed import BinancePriceFeed
from feed.fills_feed         import FillsFeed
from indicators.engine  import compute
from strategy.signal    import evaluate, SignalType
from risk.calculator    import (
    RiskLevels, TrailState,
    calc_levels, recalc_levels_from_fill, calc_real_pl, calc_gross_pl,
)
from monitor.trail_loop import TrailMonitor
from orders.manager     import OrderManager
from infra.telegram            import Telegram
from infra.telegram_controller import TelegramController, EngineState
from infra.whatsapp            import WhatsApp
# from infra.whatsapp_controller import WhatsAppController  # disabled
from infra.journal             import Journal
from risk.lot_sizing           import btc_to_lots
import server as _dashboard
import threading as _threading
import infra.heartbeat as _heartbeat

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

MAX_ENTRY_SLIP_ATR_FRAC = float(os.environ.get("MAX_ENTRY_SLIP_ATR_FRAC", "0.3"))

# ══════════════════════════════════════════════════════════════════════════════
# ShivaSniperBot
# ══════════════════════════════════════════════════════════════════════════════

class ShivaSniperBot:
    def __init__(self) -> None:
        self._order_mgr = OrderManager()
        self._telegram  = Telegram()
        self._whatsapp  = WhatsApp()
        self._journal   = Journal()

        self._state    = EngineState(running=True)
        if TELEGRAM_ENABLED:
            self._tg_ctrl = TelegramController(
                engine_state = self._state,
                telegram     = self._telegram,
                journal      = self._journal,
                order_mgr    = self._order_mgr,
            )
        else:
            self._tg_ctrl = None
        # self._wa_ctrl  = WhatsAppController(  # disabled
        #     engine_state = self._state,
        #     whatsapp     = self._whatsapp,
        #     journal      = self._journal,
        #     order_mgr    = self._order_mgr,
        # )

        # ALERT_QTY (.env) is now the single source of truth for trade size.
        self._qty_lots = ALERT_QTY

        _dashboard.init(self._journal)
        self._trail_mon = TrailMonitor(
            order_mgr = self._order_mgr,
            telegram  = self._telegram,
            journal   = self._journal,
        )
        self._feed: Optional[CandleFeed] = None
        self._binance_px_feed: Optional[BinancePriceFeed] = None
        self._fills_feed: Optional[FillsFeed] = None

        self._in_position : bool                  = False
        self._risk        : Optional[RiskLevels]  = None
        self._trail_state : Optional[TrailState]  = None
        self._signal_type : str                   = "None"
        self._entry_bar_boundary_ms : int         = 0   # FIX-9: next bar open after entry

        # Guards
        self._entry_lock  = asyncio.Lock()
        self._historical_sync_done = False  # NEW: Guard for startup phantom trades

    # ── Startup ───────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        logger.info("═" * 70)
        logger.info("  Shiva Sniper Bot v10 — Starting")
        logger.info(f"  Symbol={SYMBOL}  TF={CANDLE_TIMEFRAME}")
        logger.info(f"  Position size: {self._qty_lots} lots (from ALERT_QTY)")
        logger.info(f"  FILTER_VOL_ENABLED={FILTER_VOL_ENABLED}  (false = full Pine parity)")
        logger.info(f"  MAX_ENTRY_SLIP_ATR_FRAC={MAX_ENTRY_SLIP_ATR_FRAC}  (SL recalc threshold)")
        logger.info("═" * 70)

        await self._order_mgr.initialize()

        try:
            existing_check = await self._order_mgr.fetch_open_position()
            if existing_check is None:
                await self._order_mgr.cancel_all_orders()
                logger.info("[STARTUP] Flat on Delta — cancelled all stale bracket orders (clean slate)")
        except Exception as e:
            logger.warning(f"[STARTUP] Bracket cleanup failed (non-fatal): {e}")

        # ── Startup recovery: adopt any pre-existing open position ─────────────
        existing = await self._order_mgr.fetch_open_position()
        
        # FIX: Validate local database vs actual exchange reality
        try:
            open_row = self._journal.get_open_trade()
            if open_row and not existing:
                logger.info("[STARTUP] Database ghost row detected but Delta Exchange is FLAT. Purging local trade memory.")
                self._journal.clear_open_trade()
        except Exception as je:
            logger.warning(f"[STARTUP] Local journal state verification anomaly: {je}")

        if existing:
            logger.warning(
                f"[STARTUP] Open position detected — will resume trail on next "
                f"bar close. is_long={existing['is_long']} "
                f"entry={existing['entry_price']:.2f}"
            )
            self._in_position = True
            self._risk = RiskLevels(
                entry_price = existing["entry_price"],
                sl          = 0.0,
                tp          = 0.0,
                stop_dist   = 0.0,
                atr         = 0.0,
                is_long     = existing["is_long"],
                is_trend    = True,
            )
            self._signal_type = "RECOVERED"
            await self._telegram.send(
                f"⚠️ <b>Position Recovery</b>\n"
                f"Bot restarted mid-trade.\n"
                f"Direction: {'LONG' if existing['is_long'] else 'SHORT'}\n"
                f"Entry (approx): {existing['entry_price']:.2f}\n"
                f"Trail management resumes on next bar close."
            )

        await self._telegram.send(
            f"🟢 <b>Shiva Sniper Bot v10 Started</b>\n"
            f"Symbol: <code>{SYMBOL}</code>  TF: <code>{CANDLE_TIMEFRAME}</code>\n"
            f"Qty: <code>{self._qty_lots} lots</code> "
            f"({POSITION_BTC_SIZE} BTC)\n"
            f"Volume filter: <code>{'ON' if FILTER_VOL_ENABLED else 'OFF (Pine parity)'}</code>"
        )

    async def shutdown(self) -> None:
        logger.info("Shutting down...")
        try:
            _dashboard.stop()
        except Exception:
            pass
        self._trail_mon.stop()
        try:
            if self._tg_ctrl: self._tg_ctrl.stop()
        except Exception:
            pass
        if self._binance_px_feed is not None:
            self._binance_px_feed.stop()
        if self._fills_feed is not None:
            self._fills_feed.stop()
        try:
            await asyncio.shield(self._telegram.send("🔴 <b>Shiva Sniper Bot Stopped</b>"))
        except Exception:
            pass
        try:
            self._journal.close()
        except Exception:
            pass
        try:
            await self._order_mgr.close_exchange()
        except Exception:
            pass
        logger.info("Shutdown complete.")

    # ── Feed callbacks ────────────────────────────────────────────────────────

    async def _feed_ready(self) -> None:
        logger.info("Feed ready — waiting for first bar close...")

    async def _on_bar_close(self, df) -> None:
        if self._in_position and not self._entry_lock.locked():
            try:
                actual = await self._order_mgr.fetch_open_position()
                if actual is None:
                    logger.warning(
                        "[BAR] State drift detected: in_position=True but Delta "
                        "is flat. Bracket SL/TP fired silently — recovering exit."
                    )

                    # FIX-10: Try to get the real fill price from Delta order/fill
                    # history before falling back to the bracket trigger price.
                    # This corrects the journal entry (was off by ~3-4 pts per lot).
                    # FIX-24 (FABRICATED-EXIT-PRICE): retry — Delta's fills
                    # endpoint lags the actual fill by 1-2s, so a single attempt
                    # fails exactly on the fast SL fires we most need a real
                    # price for. If it still can't be resolved, the exit price
                    # below is a GUESS and must be labelled as such rather than
                    # written to the journal as if it were measured.
                    real_fill: Optional[float] = None
                    for _attempt in range(1, 4):
                        try:
                            real_fill = await self._order_mgr.fetch_bracket_fill_price()
                            if real_fill is not None and float(real_fill) > 0:
                                break
                            real_fill = None
                        except Exception as fill_err:
                            logger.warning(
                                f"[BAR] fetch_bracket_fill_price attempt "
                                f"{_attempt}/3 failed: {fill_err}"
                            )
                        if _attempt < 3:
                            await asyncio.sleep(1.0 * _attempt)

                    exit_is_estimated = real_fill is None

                    if real_fill is not None:
                        exit_price = float(real_fill)
                        logger.info(f"[BAR] Drift recovery: using real fill price {exit_price:.2f}")
                    elif self._trail_state is not None:
                        exit_price = float(self._trail_state.current_sl)
                        logger.info(f"[BAR] Drift recovery: using trail SL as exit price {exit_price:.2f}")
                    elif self._risk is not None and self._risk.sl > 0:
                        exit_price = float(self._risk.sl)
                        logger.info(f"[BAR] Drift recovery: using risk.sl as exit price {exit_price:.2f}")
                    else:
                        try:
                            exit_price = float(df["close"].iloc[-1])
                        except Exception:
                            exit_price = 0.0
                        logger.info(f"[BAR] Drift recovery: using bar close as exit price {exit_price:.2f}")

                    if exit_is_estimated:
                        logger.critical(
                            f"[BAR] ⚠️ FIX-24: real bracket fill unresolved — logging "
                            f"{exit_price:.2f} as an ESTIMATE. Exclude this trade from "
                            f"TradingView comparison and parameter tuning."
                        )
                        try:
                            await self._telegram.send(
                                "⚠️ <b>Estimated exit price</b>\n"
                                "Bracket fired but the real fill could not be fetched.\n"
                                f"Logged <code>{exit_price:.2f}</code> as an ESTIMATE — "
                                "verify on Delta before trusting this trade's P&L."
                            )
                        except Exception:
                            pass

                    if self._trail_mon._running:
                        self._trail_mon.stop()

                    try:
                        await self._on_trail_exit(
                            exit_price = exit_price,
                            reason     = (
                                "Bracket SL/TP (ESTIMATED — fill unresolved)"
                                if exit_is_estimated
                                else "Bracket SL/TP (recovered)"
                            ),
                            source     = "drift-check",
                            position_already_closed = True,
                        )
                    except Exception as exit_err:
                        logger.error(f"[BAR] Drift-recovery exit failed: {exit_err}", exc_info=True)
                        self._in_position = False
                        self._risk        = None
                        self._trail_state = None
                        self._signal_type = "None"
            except Exception as e:
                logger.warning(f"[BAR] State sanity check failed: {e}")

        # ── 1. Compute indicators ─────────────────────────────────────────────
        try:
            snap = compute(df)
        except ValueError as e:
            logger.warning(f"[BAR] Not enough bars: {e}")
            return

        logger.info(
            f"[BAR] close={snap.close:.2f}  atr={snap.atr:.2f}  "
            f"adx={snap.adx:.1f}  rsi={snap.rsi:.1f}  "
            f"trend={snap.trend_regime}  range={snap.range_regime}  "
            f"filters={'OK' if snap.filters_ok else 'FAIL'}  "
            f"[atr={snap.atr_ok} body={snap.body_ok} vol={snap.vol_ok}]"
        )

        # FIX-REV: Pre-compute the raw signal candidate regardless of whether
        # we're in a position. Needed both for the normal flat-entry path
        # (section 3) AND to detect an opposite-direction reversal signal
        # while a trade is still open (section 2 below). Previously this was
        # only ever computed with has_position gating it to NONE whenever
        # in_position=True, so a genuine opposite signal from Pine's engine
        # was silently dropped — the bot just kept waiting for SL/TP/trail
        # while Pine had already auto-reversed the position on that bar.
        _candidate_sig = evaluate(snap, has_position=False)

        # SHADOW-LOG (spec §6C): record this bar's decision for paper-vs-backtest
        # divergence tracking. No-op unless SHADOW_LOG_ENABLED=true; never raises.
        try:
            from infra.shadow_logger import log_bar as _shadow_log_bar
            _shadow_log_bar(snap, _candidate_sig, self._in_position)
        except Exception:
            pass

        # ── 2. Trail update for open position ─────────────────────────────────
        if self._in_position:
            if self._trail_mon._running:
                # FIX-9: is_entry_bar=True on the bar where entry was taken.
                # Pine never evaluates SL/TP on the signal bar itself.
                _is_entry_bar = (
                    self._entry_bar_boundary_ms > 0
                    and int(snap.timestamp) + 1 <= self._entry_bar_boundary_ms
                )
                self._trail_mon.on_bar_close(
                    bar_close   = snap.close,
                    bar_high    = snap.high,
                    bar_low     = snap.low,
                    bar_open    = snap.open,
                    current_atr = snap.atr,
                    is_entry_bar = _is_entry_bar,
                )

                # FIX-REV: Match Pine's strategy.entry() auto-reverse behaviour.
                # If an opposite-direction signal fired on this bar close and
                # we're still holding the old-direction trade (SL/trail hasn't
                # closed it yet), force-close now and fall through to section 3
                # so the new-direction entry can be taken on the SAME bar —
                # exactly like Pine does. Same-direction or NONE signals are
                # left alone (no pyramiding, matches existing behaviour).
                if (
                    ALLOW_REVERSAL
                    and not _is_entry_bar
                    and self._risk is not None
                    and _candidate_sig.signal_type != SignalType.NONE
                    and _candidate_sig.is_long != self._risk.is_long
                    and not self._entry_lock.locked()
                ):
                    logger.info(
                        f"[REVERSAL] Opposite signal {_candidate_sig.signal_type.value} "
                        f"detected while in {'LONG' if self._risk.is_long else 'SHORT'} "
                        f"position — force-closing to match Pine's auto-reverse."
                    )
                    await self._force_close_for_reversal(snap)
                    # Do NOT return — fall through to section 3 below so the
                    # new-direction entry can be evaluated on this same bar.
                else:
                    return
            else:
                if self._risk is not None and self._risk.stop_dist == 0.0:
                    open_row = None
                    try:
                        open_row = self._journal.get_open_trade()
                    except Exception as _je:
                        logger.warning(f"[RECOVERY] Journal read failed: {_je}")

                    if open_row and open_row.get("sl", 0) > 0 and open_row.get("atr", 0) > 0:
                        _orig_sl  = float(open_row["sl"])
                        _orig_tp  = float(open_row["tp"])
                        _orig_atr = float(open_row["atr"])
                        _atr_mult = TREND_ATR_MULT if self._risk.is_trend else RANGE_ATR_MULT
                        
                        if self._risk.is_long:
                            _signal_close = _orig_sl + _atr_mult * _orig_atr
                        else:
                            _signal_close = _orig_sl - _atr_mult * _orig_atr
                            
                        rebuilt = RiskLevels(
                            entry_price    = self._risk.entry_price,
                            sl             = _orig_sl,
                            tp             = _orig_tp,
                            stop_dist      = abs(_orig_sl - self._risk.entry_price),
                            atr            = _orig_atr,
                            is_long        = self._risk.is_long,
                            is_trend       = self._risk.is_trend,
                            signal_close   = _signal_close,
                        )
                        current_sl = float(open_row.get("current_sl", open_row["sl"]))
                    else:
                        rebuilt = calc_levels(
                            entry_price = self._risk.entry_price,
                            atr         = snap.atr,
                            is_long     = self._risk.is_long,
                            is_trend    = self._risk.is_trend,
                        )
                        rebuilt = recalc_levels_from_fill(rebuilt, self._risk.entry_price)
                        current_sl = rebuilt.sl

                    self._risk        = rebuilt
                    # FIX-21: keep OrderManager's slippage-check ATR in sync —
                    # it only updates on a fresh open_position() call, which a
                    # recovered position never makes.
                    self._order_mgr.set_atr(rebuilt.atr)
                    from config import TRAIL_STAGES as _TS, PINE_MINTICK as _MT
                    _t1_dist = rebuilt.atr * _TS[0][1] * _MT
                    _pine_init_sl = (rebuilt.entry_price + _t1_dist) if not rebuilt.is_long else (rebuilt.entry_price - _t1_dist)
                    _rec_stage = int(open_row.get("trail_stage", 0)) if open_row else 0
                    self._trail_state = TrailState(
                        stage      = _rec_stage,
                        current_sl = current_sl if _rec_stage > 0 else _pine_init_sl,
                        peak_price = self._risk.entry_price,
                    )

                    original_wall_ms: Optional[int] = None
                    try:
                        if open_row and open_row.get("opened_at"):
                            from datetime import datetime, timezone as _tz
                            dt = datetime.fromisoformat(str(open_row["opened_at"]))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=_tz.utc)
                            original_wall_ms = int(dt.timestamp() * 1000)
                    except Exception as _te:
                        pass

                    # FIX-23 (RECOVERY-OFFSET-POISONING): is_recovery=True tells
                    # TrailMonitor that rebuilt.entry_price is a HISTORICAL fill
                    # (seeded above from fetch_open_position at startup), so it
                    # must never be used as the Binance→Delta offset reference.
                    # Without this the first Binance tick locked
                    # offset = binance_now - entry_then = unrealised P&L, and
                    # RECAL_MAX_JUMP=10 then blocked the correction for the whole
                    # life of the trade.
                    self._trail_mon.start(
                        risk_levels       = rebuilt,
                        trail_state       = self._trail_state,
                        entry_bar_time_ms = original_wall_ms if original_wall_ms is not None else int(time.time() * 1000),
                        on_trail_exit     = self._on_trail_exit,
                        entry_wall_ms     = original_wall_ms,
                        qty               = self._qty_lots,
                        is_recovery       = True,
                    )
                    await self._telegram.send(f"♻️ <b>Trail Resumed (Recovery)</b>\nEntry: {rebuilt.entry_price:.2f}")
            return

        # ── 3. Evaluate entry signals (only when flat) ────────────────────────
        # Reuses _candidate_sig computed in section 1 (also used above for
        # reversal detection) rather than recomputing — same result either way.
        sig = _candidate_sig

        # FIX: Historical Boot Guard
        is_historical_boot = not self._historical_sync_done
        self._historical_sync_done = True

        if sig.signal_type == SignalType.NONE:
            return

        # NEW GUARD LOGIC: Ignore signals printed on the startup bar payload
        if is_historical_boot:
            logger.info(
                f"[STARTUP GUARD] Strategy math detected {sig.signal_type.value} on the downloaded history. "
                f"Ignoring past signal to ensure Pine Parity. Bot will only enter on new live candles."
            )
            return

        if not self._state.running:
            logger.info(f"[SIGNAL] {sig.signal_type.value} ignored — engine PAUSED via /stop_bot")
            return

        logger.info(f"[SIGNAL] {sig.signal_type.value}  is_long={sig.is_long}  regime={sig.regime}")

        # ── 4. Place entry ─────────────────────────────────────────────────────
        if self._entry_lock.locked():
            return

        async with self._entry_lock:
            if self._in_position:
                return

            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend, entry_bar_open=snap.open, signal_close=snap.close)

            try:
                order = await self._order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                    atr     = snap.atr,
                    qty     = self._qty_lots,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Order failed: {e}")
                await self._telegram.send(f"❌ <b>Entry Order FAILED</b>\nSignal: {sig.signal_type.value}\nError: <code>{e}</code>")
                return

            fill = float(order.get("average") or order.get("price") or snap.close)

            # FIX-QTY: Read actual filled contracts from the order response.
            # self._qty_lots is pre-computed from POSITION_BTC_SIZE (e.g. 3 lots),
            # but Delta may fill a different size (e.g. ALERT_QTY=13 contracts).
            # The order response carries the real filled amount under "amount",
            # "filled", or "contracts". Use that if > 0, else fall back to the
            # pre-computed value so recovery/manual trades still work.
            _filled_contracts = (
                float(order.get("filled") or order.get("amount") or order.get("contracts") or 0)
            )
            if _filled_contracts > 0 and abs(_filled_contracts - self._qty_lots) > 0.01:
                logger.info(
                    f"[QTY-FIX] Using actual fill qty={_filled_contracts:.0f} contracts "
                    f"(pre-computed was {self._qty_lots} lots from POSITION_BTC_SIZE)"
                )
                self._qty_lots = int(round(_filled_contracts))
            else:
                logger.debug(
                    f"[QTY-FIX] Fill qty={_filled_contracts:.0f} matches pre-computed "
                    f"{self._qty_lots} lots — no correction needed"
                )

            # FIX-SLIP-DIRECTION (2026-07-31): the old formula measured slip in
            # the P&L-favourable direction, which is the OPPOSITE of the stop
            # buffer direction. SL is anchored to signal_close, not fill, so:
            #   Long : SL = anchor - stopDist (fixed low).  Buffer to SL from
            #          fill = fill - SL. Buffer SHRINKS when fill < anchor.
            #   Short: SL = anchor + stopDist (fixed high). Buffer to SL from
            #          fill = SL - fill. Buffer SHRINKS when fill > anchor.
            # So "bad" slip (buffer shrinking) is (anchor - fill) for longs and
            # (fill - anchor) for shorts — exactly reversed from the old code.
            slip = (snap.close - fill) if sig.is_long else (fill - snap.close)
            slip_limit = snap.atr * MAX_ENTRY_SLIP_ATR_FRAC

            if slip > slip_limit:
                # FIX-SLIP-NOOP (2026-07-31): passing signal_close here made this
                # a no-op -- calc_levels() anchors to signal_close whenever it is
                # > 0, so the "recalculated" SL/TP came out byte-identical to
                # risk_pre above. Omitting signal_close anchors to the actual
                # fill instead, restoring the intended ATR stop distance around
                # the real entry. This is a deliberate, logged departure from
                # strict Pine-anchor behaviour -- it only fires when slippage
                # already broke Pine parity for this trade.
                logger.warning(
                    f"[ENTRY-SLIP] {slip:.2f}pts buffer shrink "
                    f"(limit={slip_limit:.2f}pts, {slip/snap.atr*100:.1f}% ATR) -- "
                    f"recalculating SL/TP anchored to fill={fill:.2f} "
                    f"instead of signal_close={snap.close:.2f}"
                )
                risk_pre = calc_levels(
                    fill, snap.atr, sig.is_long, sig.is_trend,
                    entry_bar_open=snap.open,
                )

            risk = RiskLevels(
                entry_price    = fill,
                sl             = risk_pre.sl,
                tp             = risk_pre.tp,
                stop_dist      = risk_pre.stop_dist,
                atr            = risk_pre.atr,
                is_long        = risk_pre.is_long,
                is_trend       = risk_pre.is_trend,
                entry_bar_open = snap.open,
                signal_close   = snap.close,
            )

            self._in_position  = True
            self._risk         = risk
            self._signal_type  = sig.signal_type.value
            self._entry_ts     = int(snap.timestamp)   # SHADOW-LOG: per-trade key
            
            # current_sl = risk.sl  (= signal_close ± ATR×atrMult, Pine-exact)
            # DO NOT use entry+trail_pts here — that is the activation distance,
            # not the initial stop loss. Using it set SL ~80 pts tighter than Pine,
            # causing instant stop-outs when price reversed before trail armed.
            self._trail_state  = TrailState(
                stage        = 0,
                current_sl   = risk.sl,   # ← correct Pine initial SL
                peak_price   = fill,
                trail_armed  = False,
                best_price   = 0.0,
            )

            self._trail_mon.start(
                risk_levels       = risk,
                trail_state       = self._trail_state,
                entry_bar_time_ms = int(time.time() * 1000),
                on_trail_exit     = self._on_trail_exit,
                signal_bar_high   = snap.high,
                signal_bar_low    = snap.low,
                signal_bar_open   = snap.open,
                signal_bar_close  = snap.close,
                qty               = self._qty_lots,
            )

            try:
                _tf_str  = CANDLE_TIMEFRAME
                _unit    = _tf_str[-1]
                _n       = int(_tf_str[:-1])
                _mult_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(_unit, 60_000)
                _period_ms      = _n * _mult_ms
                _next_bar_open  = int(snap.timestamp) + _period_ms
                self._entry_bar_boundary_ms = _next_bar_open  # FIX-9: track for is_entry_bar
                self._trail_mon.set_entry_bar_boundary(_next_bar_open)
            except Exception as _gge:
                pass

            logger.info(
                f"[ENTRY] Filled | type={sig.signal_type.value}  "
                f"fill={fill:.2f}  sl={risk.sl:.2f}  tp={risk.tp:.2f}  "
                f"atr={snap.atr:.2f}  stop_dist={risk.stop_dist:.2f}"
            )

            try:
                self._journal.open_trade(
                    signal_type = sig.signal_type.value,
                    is_long     = sig.is_long,
                    entry_price = fill,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = snap.atr,
                    qty         = self._qty_lots,
                )
            except Exception:
                pass

            await self._telegram.notify_entry(
                signal_type = sig.signal_type.value,
                entry_price = fill,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
                qty         = self._qty_lots,
            )

    async def _force_close_for_reversal(self, snap) -> None:
        """
        FIX-REV: Force-close the current position on an opposite-direction
        signal, mirroring Pine's strategy.entry() same-bar reversal.

        Pine has no concept of "wait for SL/TP" when a new opposite signal
        fires — it closes the existing trade at market and opens the new one,
        same bar. Our bot only ever exited via TP/Trail-SL/Max-SL/BE, so a
        reversal signal was previously invisible to it while in a position.
        This restores that behaviour, reusing the existing close_position()
        (reduce-only market order) and _on_trail_exit() bookkeeping path so
        journaling/Telegram/state-reset all stay consistent with every other
        exit reason.
        """
        if not self._in_position or self._risk is None:
            return

        was_long = self._risk.is_long

        if self._trail_mon._running:
            self._trail_mon.stop()

        exit_price = snap.close
        try:
            order = await self._order_mgr.close_position(
                is_long        = was_long,
                reason         = "Reversal signal",
                expected_price = snap.close,
                qty            = self._qty_lots,
            )
            _fill = order.get("average") or order.get("price")
            if _fill:
                exit_price = float(_fill)
        except Exception as e:
            logger.error(f"[REVERSAL] close_position failed: {e}", exc_info=True)
            # Fall back to bar-close as the recorded exit price. If the
            # exchange-side close actually failed (rather than just the fill
            # lookup), the next bar's drift-check (section at top of
            # _on_bar_close) will detect the exchange/journal mismatch and
            # reconcile it — same safety net used for silent bracket fires.

        await self._on_trail_exit(
            exit_price = exit_price,
            reason     = "Reversal Signal",
            source     = "reversal",
            position_already_closed = True,
        )

    async def _on_trail_exit(self, exit_price: float, reason: str, source: str = "tick", position_already_closed: bool = False) -> None:
        if not self._in_position:
            return

        if not position_already_closed:
            logger.warning(
                f"[EXIT] ⚠️  _on_trail_exit called with position_already_closed=False "
                f"— reason={reason} source={source}. "
            )

        risk = self._risk
        pl   = (calc_gross_pl(risk.entry_price, exit_price, risk.is_long, self._qty_lots) if risk else 0.0)

        logger.info(
            f"[EXIT] reason={reason}  source={source}  "
            f"entry={risk.entry_price if risk else '?'}  "
            f"exit={exit_price:.2f}  gross_pl={pl:+.6f} USD"
        )

        try:
            if risk:
                self._journal.log_trade(
                    signal_type = self._signal_type,
                    is_long     = risk.is_long,
                    entry_price = risk.entry_price,
                    exit_price  = exit_price,
                    sl          = risk.sl,
                    tp          = risk.tp,
                    atr         = risk.atr,
                    qty         = self._qty_lots,
                    real_pl     = pl,
                    exit_reason = reason,
                    trail_stage = self._trail_state.stage if self._trail_state else 0,
                )
                self._journal.close_open_trade()
        except Exception as e:
            logger.warning(f"[JOURNAL] log_trade failed: {e}")

        # SHADOW-LOG (spec §6C): record the exit + realized P/L for per-trade
        # paper-vs-backtest comparison. No-op unless SHADOW_LOG_ENABLED=true;
        # never raises.
        try:
            from infra.shadow_logger import log_event as _shadow_log_event
            _shadow_log_event(
                "exit",
                entry_ts    = int(getattr(self, "_entry_ts", 0)),
                signal_type = self._signal_type,
                is_long     = (risk.is_long if risk else None),
                entry_price = round(float(risk.entry_price), 2) if risk else 0.0,
                exit_price  = round(float(exit_price), 2),
                sl          = round(float(risk.sl), 2) if risk else 0.0,
                tp          = round(float(risk.tp), 2) if risk else 0.0,
                real_pl     = round(float(pl), 6),
                exit_reason = reason,
            )
        except Exception:
            pass

        try:
            await self._telegram.notify_exit(
                reason      = reason,
                entry_price = risk.entry_price if risk else 0.0,
                exit_price  = exit_price,
                real_pl     = pl,
                is_long     = risk.is_long if risk else True,
                qty         = self._qty_lots,
            )
        except Exception:
            pass

        self._in_position  = False
        self._risk         = None
        self._trail_state  = None
        self._signal_type  = "None"

    async def run(self) -> None:
        await self.initialize()

        if self._tg_ctrl: self._tg_ctrl_task = asyncio.create_task(self._tg_ctrl.run())
        # self._wa_ctrl_task = asyncio.create_task(self._wa_ctrl.run())  # disabled

        feed = CandleFeed(
            on_bar_close  = self._on_bar_close,
            on_feed_ready = self._feed_ready,
        )
        feed.trail_monitor = self._trail_mon
        self._trail_mon.set_feed(feed)   # FIX-ARM-BUFFER: expose feed gap to trail arm check
        self._feed = feed

        if os.environ.get("USE_BINANCE_FEED", "true").lower() == "true":
            self._binance_px_feed = BinancePriceFeed(self._trail_mon)
            self._binance_px_feed.start_task()

        self._fills_feed = FillsFeed(
            trail_monitor = self._trail_mon,
            order_manager = self._order_mgr,
        )
        self._fills_feed.start_task()

        _dashboard.start()
        _start_client_dashboard()
        try:
            await feed.start()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

def _start_client_dashboard() -> None:
    """
    Launch the client / billing FastAPI dashboard (dashboard/main.py) on port 8080
    in a daemon thread so it doesn't block the async event loop.
    """
    import importlib
    import logging as _log

    logger = _log.getLogger(__name__)
    try:
        import uvicorn
        from dashboard.main import app as _client_app

        cfg = uvicorn.Config(
            _client_app,
            host="0.0.0.0",
            port=int(__import__("os").environ.get("CLIENT_DASHBOARD_PORT", "8080")),
            log_level="warning",
        )
        server = uvicorn.Server(cfg)

        def _run():
            import asyncio
            asyncio.run(server.serve())

        t = _threading.Thread(target=_run, daemon=True, name="client-dashboard")
        t.start()
        logger.info("Client dashboard LIVE → http://0.0.0.0:8080")
    except Exception as exc:
        logger.warning(f"[CLIENT DASH] Could not start client dashboard: {exc}")


async def _main() -> None:
    _heartbeat.start(os.path.dirname(os.path.abspath(__file__)))
    bot  = ShivaSniperBot()
    loop = asyncio.get_running_loop()

    def _handle_signal(sig_num: int) -> None:
        for task in asyncio.all_tasks(loop):
            if task.get_name() != "bot_run":
                task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda sn=s: _handle_signal(sn))
        except NotImplementedError:
            pass 

    run_task = asyncio.create_task(bot.run(), name="bot_run")
    await run_task

if __name__ == "__main__":
    asyncio.run(_main())

from orders.manager     import OrderManager, build_exchange          # noqa: E402,F401
from monitor.trail_loop import TrailMonitor                          # noqa: E402,F401
from indicators.engine  import IndicatorSnapshot, Signal, SignalType # noqa: E402,F401
from risk.calculator    import RiskLevels, TrailState                # noqa: E402,F401
from execution import ExecutionEngine, log_signal                    # noqa: E402,F401
# This won't work appended — need to patch properly
