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
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME, FILTER_VOL_ENABLED,
    POSITION_BTC_SIZE, TREND_ATR_MULT, RANGE_ATR_MULT,
)
from feed.ws_feed            import CandleFeed
from feed.binance_price_feed import BinancePriceFeed
from feed.fills_feed         import FillsFeed
from indicators.engine  import compute
from strategy.signal    import evaluate, SignalType
from risk.calculator    import (
    RiskLevels, TrailState,
    calc_levels, recalc_levels_from_fill, calc_real_pl, calc_gross_pl,
    # NOTE: recalc_levels_from_fill is used ONLY in the startup recovery path
    # (mid-trade bot restart). It is intentionally NOT called for new entries.
    # PINE-PARITY-SL: new entry SL/TP anchored to snap.close, not fill price.
)
from monitor.trail_loop import TrailMonitor
from orders.manager     import OrderManager
from infra.telegram            import Telegram
from infra.telegram_controller import TelegramController, EngineState
from infra.whatsapp            import WhatsApp
from infra.whatsapp_controller import WhatsAppController
from infra.journal             import Journal
from risk.lot_sizing           import btc_to_lots
import server as _dashboard

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

# ── Slippage guard ─────────────────────────────────────────────────────────────
# If the actual fill price differs from the signal bar close by more than
# MAX_ENTRY_SLIP_ATR_FRAC × ATR, the SL anchored to signal-bar close would
# leave almost no room between fill and SL — causing instant stop-outs.
# In that case we recalculate SL/TP from the actual fill price instead.
#
# Example (Trade 2 from logs):
#   Signal close = 77,773  ATR = 238  Limit = 238 × 0.3 = 71 pts
#   Actual fill  = 77,957  Slip = 184 pts  → recalc from fill
#   New SL       = 77,957 + 238×0.9 = 78,171  (room = 214 pts, not 31)
#
# Set via env var MAX_ENTRY_SLIP_ATR_FRAC (default 0.3 = 30% of ATR).
MAX_ENTRY_SLIP_ATR_FRAC = float(os.environ.get("MAX_ENTRY_SLIP_ATR_FRAC", "0.3"))


# ══════════════════════════════════════════════════════════════════════════════
# ShivaSniperBot
# ══════════════════════════════════════════════════════════════════════════════

class ShivaSniperBot:
    """
    Live bot orchestrator.

    Lifecycle:
      initialize()        — connect exchange, restore any open position
      _feed_ready()       — called once by CandleFeed after history is loaded
      _on_bar_close(df)   — called on every confirmed bar close (WS or REST)
      _on_trail_exit(...) — called by TrailMonitor when position is closed
      shutdown()          — graceful stop (Telegram, journal, exchange)
    """

    def __init__(self) -> None:
        self._order_mgr = OrderManager()
        self._telegram  = Telegram()
        self._whatsapp  = WhatsApp()
        self._journal   = Journal()

        # v10: shared run-state flag + Telegram command controller
        self._state    = EngineState(running=True)
        self._tg_ctrl  = TelegramController(
            engine_state = self._state,
            telegram     = self._telegram,
            journal      = self._journal,
            order_mgr    = self._order_mgr,
        )
        self._wa_ctrl  = WhatsAppController(
            engine_state = self._state,
            whatsapp     = self._whatsapp,
            journal      = self._journal,
            order_mgr    = self._order_mgr,
        )

        # v10: convert configured BTC size → Delta lots (0.1 BTC = 100 lots)
        # Falls back to ALERT_QTY if POSITION_BTC_SIZE is 0/unset.
        try:
            self._qty_lots = btc_to_lots(POSITION_BTC_SIZE) if POSITION_BTC_SIZE > 0 else ALERT_QTY
        except Exception as e:
            logger.warning(f"btc_to_lots failed ({e}) — falling back to ALERT_QTY={ALERT_QTY}")
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

        # Position state — reset on each exit
        self._in_position : bool                  = False
        self._risk        : Optional[RiskLevels]  = None
        self._trail_state : Optional[TrailState]  = None
        self._signal_type : str                   = "None"

        # Guards
        self._entry_lock  = asyncio.Lock()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Connect to exchange, log config, restore open position if any."""
        logger.info("═" * 70)
        logger.info("  Shiva Sniper Bot v10 — Starting")
        logger.info(f"  Symbol={SYMBOL}  TF={CANDLE_TIMEFRAME}")
        logger.info(f"  Position size: {POSITION_BTC_SIZE} BTC → {self._qty_lots} lots")
        logger.info(f"  FILTER_VOL_ENABLED={FILTER_VOL_ENABLED}  (false = full Pine parity)")
        logger.info(f"  MAX_ENTRY_SLIP_ATR_FRAC={MAX_ENTRY_SLIP_ATR_FRAC}  (SL recalc threshold)")
        logger.info("═" * 70)

        await self._order_mgr.initialize()

        # ── FIX-BRACKET-CLEANUP: Cancel orphaned bracket orders on startup ─────
        # If bot crashed after placing a bracket but before logging the exit,
        # stale bracket orders remain on Delta. On the next trade attempt this
        # causes bracket_order_exists (400) errors. Clean them up if we are flat.
        try:
            existing_check = await self._order_mgr.fetch_open_position()
            if existing_check is None:
                await self._order_mgr.cancel_all_orders()
                logger.info("[STARTUP] Flat on Delta — cancelled all stale bracket orders (clean slate)")
            else:
                logger.info(
                    f"[STARTUP] Open position found — skipping bracket cancel. "
                    f"entry={existing_check.get('entry_price', '?')}"
                )
        except Exception as e:
            logger.warning(f"[STARTUP] Bracket cleanup failed (non-fatal): {e}")

        # ── Startup recovery: adopt any pre-existing open position ─────────────
        existing = await self._order_mgr.fetch_open_position()
        if existing:
            logger.warning(
                f"[STARTUP] Open position detected — will resume trail on next "
                f"bar close. is_long={existing['is_long']} "
                f"entry={existing['entry_price']:.2f}"
            )
            # Build placeholder RiskLevels — SL/TP reconstructed on first bar close
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
            await self._whatsapp.send(
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
        await self._whatsapp.send(
            f"🟢 <b>Shiva Sniper Bot v10 Started</b>\n"
            f"Symbol: <code>{SYMBOL}</code>  TF: <code>{CANDLE_TIMEFRAME}</code>\n"
            f"Qty: <code>{self._qty_lots} lots</code> "
            f"({POSITION_BTC_SIZE} BTC)\n"
            f"Volume filter: <code>{'ON' if FILTER_VOL_ENABLED else 'OFF (Pine parity)'}</code>"
        )

    async def shutdown(self) -> None:
        """Graceful stop — stop trail, close exchange connection, notify."""
        logger.info("Shutting down...")
        # Release the dashboard port FIRST so pm2 can rebind on restart
        # without hitting "Address already in use".
        try:
            _dashboard.stop()
        except Exception:
            pass
        self._trail_mon.stop()
        # v10: stop Telegram controller
        try:
            self._tg_ctrl.stop()
        except Exception:
            pass
        if self._binance_px_feed is not None:
            self._binance_px_feed.stop()
        if self._fills_feed is not None:
            self._fills_feed.stop()
        # Use asyncio.shield() so these sends survive task cancellation.
        # Without shield, the signal handler's task.cancel() kills the
        # aiohttp request mid-flight and the WhatsApp stop message is lost.
        try:
            await asyncio.shield(self._telegram.send("🔴 <b>Shiva Sniper Bot Stopped</b>"))
        except Exception:
            pass
        try:
            await asyncio.shield(self._whatsapp.send("🔴 *Shiva Sniper Bot Stopped*"))
        except Exception:
            pass
        try:
            self._wa_ctrl.stop()
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
        """Called by CandleFeed once historical bars are loaded."""
        logger.info("Feed ready — waiting for first bar close...")

    async def _on_bar_close(self, df) -> None:
        """
        Called by CandleFeed on every confirmed bar close.

        Pine parity: calc_on_every_tick=false means:
          - Entry signals only fire at confirmed bar close.
          - Stage upgrades + BE only fire at bar close.
          - TP / SL / Trail SL checked at bar close AND intrabar (WS ticks).
        """
        # ── 0. State sanity check ─────────────────────────────────────────────
        # FIX-BRACKET-RECOVERY (2026-05-11):
        # When SL_FIRE_VIA_BRACKET=true, the Python tick loop intentionally
        # does NOT fire market closes on SL crosses — Delta's bracket SL
        # handles them at matching-engine speed. The side effect: when the
        # bracket fires, Python has no idea the position closed. No Telegram
        # exit, no journal row, no P&L logged — the trade simply disappears.
        #
        # This block detects that drift (memory says in_position, Delta says
        # flat) and routes through the normal _on_trail_exit() path so the
        # Telegram exit, journal entry, and P&L all get recorded properly.
        #
        # Best-effort exit price = last trailed SL (most accurate for bracket
        # SL fills), falling back to original risk SL, then bar close.
        if self._in_position and not self._entry_lock.locked():
            try:
                actual = await self._order_mgr.fetch_open_position()
                if actual is None:
                    logger.warning(
                        "[BAR] State drift detected: in_position=True but Delta "
                        "is flat. Bracket SL/TP fired silently — recovering exit."
                    )

                    # Determine best-effort exit price.
                    exit_price: float
                    if self._trail_state is not None:
                        exit_price = float(self._trail_state.current_sl)
                    elif self._risk is not None and self._risk.sl > 0:
                        exit_price = float(self._risk.sl)
                    else:
                        # Last resort: use current bar close.
                        try:
                            exit_price = float(df["close"].iloc[-1])
                        except Exception:
                            exit_price = 0.0

                    # Stop trail BEFORE firing exit so it doesn't double-fire
                    # on the next price tick.
                    if self._trail_mon._running:
                        self._trail_mon.stop()

                    # Route through the normal exit path — Telegram, journal,
                    # and P&L are all handled inside _on_trail_exit, which
                    # also resets in_position / risk / trail_state.
                    try:
                        await self._on_trail_exit(
                            exit_price = exit_price,
                            reason     = "Bracket SL/TP (recovered)",
                            source     = "drift-check",
                            position_already_closed = True,  # Delta confirmed flat above
                        )
                    except Exception as exit_err:
                        logger.error(
                            f"[BAR] Drift-recovery exit failed: {exit_err}",
                            exc_info=True,
                        )
                        # Hard reset so we don't get stuck.
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
            f"filters={'OK' if snap.filters_ok else 'FAIL'}"
            f"  [atr={snap.atr_ok} body={snap.body_ok} vol={snap.vol_ok}]"
        )

        # ── 2. Trail update for open position ─────────────────────────────────
        if self._in_position:
            if self._trail_mon._running:
                # Normal: trail is active — update with bar data
                self._trail_mon.on_bar_close(
                    bar_close   = snap.close,
                    bar_high    = snap.high,
                    bar_low     = snap.low,
                    bar_open    = snap.open,
                    current_atr = snap.atr,
                )
            else:
                # Recovery: bot was restarted mid-trade — reconstruct and start trail
                if self._risk is not None and self._risk.stop_dist == 0.0:
                    # FIX-RECOVERY: use the original SL/TP/ATR stored in the journal.
                    # Previous code recomputed from live ATR — this destroys Pine parity
                    # because ATR drifts after entry. A trade entered with ATR=246 but
                    # recovered with ATR=233 gets an SL 28pts tighter than Pine expects,
                    # causing premature stop-outs on normal retracements.
                    open_row = None
                    try:
                        open_row = self._journal.get_open_trade()
                    except Exception as _je:
                        logger.warning(f"[RECOVERY] Journal read failed: {_je}")

                    if open_row and open_row.get("sl", 0) > 0 and open_row.get("atr", 0) > 0:
                        # Journal has the original SL/TP/ATR — restore exactly
                        logger.warning(
                            f"[RECOVERY] Restoring original SL/TP/ATR from journal. "
                            f"entry={self._risk.entry_price:.2f}  "
                            f"sl={open_row['sl']:.2f}  tp={open_row['tp']:.2f}  "
                            f"atr={open_row['atr']:.2f}  current_sl={open_row['current_sl']:.2f}"
                        )
                        _orig_sl  = float(open_row["sl"])
                        _orig_tp  = float(open_row["tp"])
                        _orig_atr = float(open_row["atr"])
                        _atr_mult = TREND_ATR_MULT if self._risk.is_trend else RANGE_ATR_MULT
                        # Reconstruct signal_close: anchor = sl - atr*mult (short)
                        # or sl + atr*mult (long). This restores Pine-parity SL anchor.
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
                        # Journal missing — fall back to live ATR (last resort)
                        logger.warning(
                            f"[RECOVERY] Journal empty — falling back to live ATR. "
                            f"entry={self._risk.entry_price:.2f}  live_atr={snap.atr:.2f}"
                        )
                        rebuilt = calc_levels(
                            entry_price = self._risk.entry_price,
                            atr         = snap.atr,
                            is_long     = self._risk.is_long,
                            is_trend    = self._risk.is_trend,
                        )
                        rebuilt = recalc_levels_from_fill(rebuilt, self._risk.entry_price)
                        current_sl = rebuilt.sl

                    self._risk        = rebuilt
                    # Recovery: recompute Pine initial SL; arm trail if already in profit
                    from config import TRAIL_STAGES as _TS, PINE_MINTICK as _MT
                    _t1_dist = rebuilt.atr * _TS[0][1] * _MT
                    _pine_init_sl = (rebuilt.entry_price + _t1_dist) if not rebuilt.is_long else (rebuilt.entry_price - _t1_dist)
                    _rec_stage = int(open_row.get("trail_stage", 0)) if open_row else 0
                    self._trail_state = TrailState(
                        stage      = _rec_stage,
                        current_sl = current_sl if _rec_stage > 0 else _pine_init_sl,
                        peak_price = self._risk.entry_price,
                    )

                    # FIX-TIME-EXIT-RECOVERY: use the original entry wall time from
                    # the journal so the 28-min clock counts from actual entry, not restart.
                    original_wall_ms: Optional[int] = None
                    try:
                        # open_row already fetched above in FIX-RECOVERY block
                        if open_row and open_row.get("opened_at"):
                            from datetime import datetime, timezone as _tz
                            dt = datetime.fromisoformat(str(open_row["opened_at"]))
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=_tz.utc)
                            original_wall_ms = int(dt.timestamp() * 1000)
                            logger.info(
                                f"[RECOVERY] Original entry time restored: "
                                f"{open_row['opened_at']} "
                                f"(elapsed={(int(time.time()*1000)-original_wall_ms)//1000}s)"
                            )
                    except Exception as _te:
                        logger.warning(f"[RECOVERY] Could not restore entry time: {_te}")

                    self._trail_mon.start(
                        risk_levels       = rebuilt,
                        trail_state       = self._trail_state,
                        entry_bar_time_ms = int(time.time() * 1000),
                        on_trail_exit     = self._on_trail_exit,
                        entry_wall_ms     = original_wall_ms,
                    )
                    await self._telegram.send(
                        f"♻️ <b>Trail Resumed (Recovery)</b>\n"
                        f"Entry: {rebuilt.entry_price:.2f}\n"
                        f"SL: {rebuilt.sl:.2f}  TP: {rebuilt.tp:.2f}\n"
                        f"ATR (live): {snap.atr:.2f}"
                    )
                    await self._whatsapp.send(
                        f"♻️ <b>Trail Resumed (Recovery)</b>\n"
                        f"Entry: {rebuilt.entry_price:.2f}\n"
                        f"SL: {rebuilt.sl:.2f}  TP: {rebuilt.tp:.2f}\n"
                        f"ATR (live): {snap.atr:.2f}"
                    )
            # In position — do NOT evaluate new entry signals
            return

        # ── 3. Evaluate entry signals (only when flat) ────────────────────────
        sig = evaluate(snap, has_position=False)

        if sig.signal_type == SignalType.NONE:
            logger.debug("[BAR] No signal.")
            return

        # v10: respect /stop_bot — skip NEW entries while paused
        if not self._state.running:
            logger.info(
                f"[SIGNAL] {sig.signal_type.value} ignored — engine PAUSED via /stop_bot"
            )
            return

        logger.info(
            f"[SIGNAL] {sig.signal_type.value}  "
            f"is_long={sig.is_long}  regime={sig.regime}"
        )

        # ── 4. Place entry ─────────────────────────────────────────────────────
        if self._entry_lock.locked():
            logger.warning("[ENTRY] Lock held — skipping duplicate attempt")
            return

        async with self._entry_lock:
            if self._in_position:
                return  # race-condition guard

            # Pre-calculate SL/TP anchored to signal bar close (Pine parity).
            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend, entry_bar_open=snap.open, signal_close=snap.close)

            try:
                order = await self._order_mgr.place_entry(
                    is_long = sig.is_long,
                    sl      = risk_pre.sl,
                    tp      = risk_pre.tp,
                )
            except Exception as e:
                logger.error(f"[ENTRY] Order failed: {e}")
                await self._telegram.send(
                    f"❌ <b>Entry Order FAILED</b>\n"
                    f"Signal: {sig.signal_type.value}\n"
                    f"Error: <code>{e}</code>"
                )
                await self._whatsapp.send(
                    f"❌ <b>Entry Order FAILED</b>\n"
                    f"Signal: {sig.signal_type.value}\n"
                    f"Error: <code>{e}</code>"
                )
                return

            fill = float(order.get("average") or order.get("price") or snap.close)

            # ── SLIPPAGE GUARD ────────────────────────────────────────────────
            # If the fill slipped more than MAX_ENTRY_SLIP_ATR_FRAC × ATR away
            # from the signal bar close, the SL anchored to signal-bar close
            # leaves dangerously little room between fill and SL.
            #
            # In that case: recalculate SL/TP from the actual fill price so
            # the stop distance is always the full ATR × mult from where we
            # actually entered — not from a bar close that was 100–200 pts away.
            #
            # We do NOT use recalc_levels_from_fill() here (that just shifts
            # the old levels by delta). We call calc_levels() fresh from fill,
            # which gives a clean ATR-based stop from the real entry price.
            #
            # Why not always do this?
            #   - Small slippage: Pine parity is preferred (SL anchored to
            #     signal close matches backtest behaviour exactly).
            #   - Large slippage: safety overrides parity — an instant stop-out
            #     is far worse than a small parity deviation.
            # Directional slip: only fire when fill is WORSE than close.
            # For a long: worse = fill above close (paid more than signal price).
            # For a short: worse = fill below close (sold lower than signal price).
            # A fill in the favourable direction means price moved for us before
            # the order landed — SL calculated from close still has full room.
            slip = (fill - snap.close) if sig.is_long else (snap.close - fill)
            slip_limit = snap.atr * MAX_ENTRY_SLIP_ATR_FRAC

            if slip > slip_limit:
                logger.warning(
                    f"[ENTRY] Slippage guard triggered: fill={fill:.2f} "
                    f"close={snap.close:.2f} slip={slip:.1f} pts "
                    f"limit={slip_limit:.1f} pts ({MAX_ENTRY_SLIP_ATR_FRAC}×ATR) — "
                    f"recalculating SL/TP from actual fill price"
                )
                risk_pre = calc_levels(
                    fill, snap.atr, sig.is_long, sig.is_trend,
                    entry_bar_open=snap.open,
                    signal_close=snap.close,  # FIX-BUG3: always remember the signal bar close
                )

            # Build final RiskLevels.
            # entry_price  = actual fill (for P&L, journal, Telegram)
            # sl / tp      = from risk_pre (signal-close anchored normally,
            #                or fill-anchored when slippage guard fired)
            risk = RiskLevels(
                entry_price    = fill,
                sl             = risk_pre.sl,
                tp             = risk_pre.tp,
                stop_dist      = risk_pre.stop_dist,
                atr            = risk_pre.atr,
                is_long        = risk_pre.is_long,
                is_trend       = risk_pre.is_trend,
                entry_bar_open = snap.open,
                signal_close   = snap.close,  # FIX-BUG3: stored so trail_loop anchors initial SL recalc here
            )

            self._in_position  = True
            self._risk         = risk
            self._signal_type  = sig.signal_type.value
            # Pine: initial trail SL = entry +/- ATR*t1Pts from tick 1
            from config import TRAIL_STAGES as _TS, PINE_MINTICK as _MT
            _t1_dist = risk.atr * _TS[0][1] * _MT
            _pine_init_sl = (risk.entry_price + _t1_dist) if not risk.is_long else (risk.entry_price - _t1_dist)
            self._trail_state  = TrailState(
                stage      = 0,
                current_sl = _pine_init_sl,
                peak_price = fill,
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
            )
            # Ghost-trade guard: compute next bar boundary from the candle timestamp.
            # snap.timestamp is the bar's open time in ms; add one bar period to get
            # the bar close / next open time.  CANDLE_TIMEFRAME e.g. "30m" → 1800000ms.
            try:
                _tf_str  = CANDLE_TIMEFRAME           # e.g. "30m", "15m", "1h"
                _unit    = _tf_str[-1]
                _n       = int(_tf_str[:-1])
                _mult_ms = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(_unit, 60_000)
                _period_ms      = _n * _mult_ms
                _next_bar_open  = int(snap.timestamp) + _period_ms
                self._trail_mon.set_entry_bar_boundary(_next_bar_open)
            except Exception as _gge:
                logger.warning(f"[ENTRY] Ghost-trade guard skipped: {_gge}")

            logger.info(
                f"[ENTRY] Filled | type={sig.signal_type.value}  "
                f"fill={fill:.2f}  sl={risk.sl:.2f}  tp={risk.tp:.2f}  "
                f"atr={snap.atr:.2f}  stop_dist={risk.stop_dist:.2f}"
            )

            # Journal
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
            except Exception as e:
                logger.warning(f"[JOURNAL] open_trade failed: {e}")

            # Telegram entry notification
            await self._telegram.notify_entry(
                signal_type = sig.signal_type.value,
                entry_price = fill,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
                qty         = self._qty_lots,
            )
            # WhatsApp entry notification
            await self._whatsapp.notify_entry(
                signal_type = sig.signal_type.value,
                entry_price = fill,
                sl          = risk.sl,
                tp          = risk.tp,
                atr         = snap.atr,
                qty         = self._qty_lots,
            )

            # ──────────────────────────────────────────────────────────────────────
            # FIX-SAME-BAR-REMOVED (2026-05-23):
            # The previous same-bar exit block was INCORRECT and caused real losses.
            #
            # Root cause: Bot enters at bar CLOSE (market order after barstate.isconfirmed).
            # The signal bar's high/low is historical — it occurred BEFORE the entry existed.
            # Checking df["high"].iloc[-1] against SL on a SHORT immediately after entry
            # always fires a false SL because the bar's wick (above entry) is in the past.
            #
            # Pine Script behaviour for bar-close entries:
            #   strategy.entry fires at bar close → strategy.exit evaluates from the
            #   NEXT bar onward. Pine never checks the entry bar's OHLC for exits when
            #   the entry was placed at barstate.isconfirmed. Confirmed by live backtester
            #   trades 397/398 on 2026-05-23 (both profitable trailing-stop exits in Pine,
            #   but bot killed them as same-bar SL losses).
            #
            # Second bug (masked by first): the same-bar path called _on_trail_exit()
            # directly — which only does bookkeeping (journal / Telegram / state reset).
            # It sends ZERO orders to Delta Exchange, leaving real open positions and
            # bracket orders orphaned on the exchange. This caused bracket_order_exists
            # errors on the next trade.
            #
            # Fix: remove the block entirely. TrailMonitor tick loop handles all exits
            # from the next price tick onward, which is correct Pine parity for
            # bar-close entries.
            # ──────────────────────────────────────────────────────────────────────

    # ── Exit callback ─────────────────────────────────────────────────────────

    async def _on_trail_exit(
        self,
        exit_price: float,
        reason    : str,
        source    : str = "tick",
        position_already_closed: bool = False,
    ) -> None:
        """Called by TrailMonitor._fire_exit() after position is closed on the exchange.

        position_already_closed=True  → Delta position is confirmed closed before
                                         this call (normal TrailMonitor path + drift check).
        position_already_closed=False → caller is NOT sure the exchange position is closed.
                                         Log a warning so the bug is visible immediately.
        """
        if not self._in_position:
            return

        # Safety guard: if this is called without a confirmed Delta close, warn loudly.
        # This catches any future code that tries to shortcut through bookkeeping only
        # (the old same-bar bug pattern — exits Python state but not the exchange).
        if not position_already_closed:
            logger.warning(
                f"[EXIT] ⚠️  _on_trail_exit called with position_already_closed=False "
                f"— reason={reason} source={source}. "
                f"Verify Delta position is actually flat before relying on this exit."
            )

        risk = self._risk
        pl   = (
            calc_gross_pl(risk.entry_price, exit_price, risk.is_long, self._qty_lots)
            if risk else 0.0
        )

        logger.info(
            f"[EXIT] reason={reason}  source={source}  "
            f"entry={risk.entry_price if risk else '?'}  "
            f"exit={exit_price:.2f}  gross_pl={pl:+.6f} USD"
        )

        # Journal
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

        # Telegram exit notification
        try:
            await self._telegram.notify_exit(
                reason      = reason,
                entry_price = risk.entry_price if risk else 0.0,
                exit_price  = exit_price,
                real_pl     = pl,
                is_long     = risk.is_long if risk else True,
                qty         = self._qty_lots,
            )
        except Exception as e:
            logger.warning(f"[TELEGRAM] notify_exit failed: {e}")
        # WhatsApp exit notification
        try:
            await self._whatsapp.notify_exit(
                reason      = reason,
                entry_price = risk.entry_price if risk else 0.0,
                exit_price  = exit_price,
                real_pl     = pl,
                is_long     = risk.is_long if risk else True,
                qty         = self._qty_lots,
            )
        except Exception as e:
            logger.warning(f"[WHATSAPP] notify_exit failed: {e}")

        # Reset — bot is flat and ready for next signal
        self._in_position  = False
        self._risk         = None
        self._trail_state  = None
        self._signal_type  = "None"

    # ── Main run loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Build feed, wire trail monitor, start feed (blocks until shutdown)."""
        await self.initialize()

        # v10: start Telegram command listener (/start_bot, /stop_bot, /status)
        self._tg_ctrl_task = asyncio.create_task(self._tg_ctrl.run())
        logger.info("[MAIN] TelegramController started — listening for /start_bot, /stop_bot, /status")

        # WhatsApp command listener (webhook on port 8080)
        self._wa_ctrl_task = asyncio.create_task(self._wa_ctrl.run())
        logger.info("[MAIN] WhatsAppController started — listening on webhook /webhook port 8080")

        feed = CandleFeed(
            on_bar_close  = self._on_bar_close,
            on_feed_ready = self._feed_ready,
        )
        # CRITICAL: wire trail_monitor so WS candle updates push price ticks
        # directly to TrailMonitor.on_price_tick() — this is the primary
        # intrabar exit detection path (FIX-PARITY-02 in trail_loop.py).
        feed.trail_monitor = self._trail_mon
        self._feed = feed

        # BINANCE-EXIT-FEED-v1: Start Binance aggTrade price feed.
        # This replaces the old Delta WS intrabar price monitoring.
        # Binance prices (~10ms) match Pine's broker emulator data source,
        # eliminating phantom SL/TP triggers from the Delta-Binance price gap.
        if os.environ.get("USE_BINANCE_FEED", "true").lower() == "true":
            self._binance_px_feed = BinancePriceFeed(self._trail_mon)
            self._binance_px_feed.start_task()
            logger.info("[MAIN] BinancePriceFeed started — exits now use Binance aggTrade prices")

        # FIX-FILLS-WS: start Delta fills WebSocket listener for instant
        # bracket exit detection. Replaces the bar-close drift check as
        # the primary bracket-exit discovery path.
        self._fills_feed = FillsFeed(
            trail_monitor = self._trail_mon,
            order_manager = self._order_mgr,
        )
        self._fills_feed.start_task()
        logger.info("[MAIN] FillsFeed started — bracket exits now detected via Delta WS fills")

        _dashboard.start()
        try:
            await feed.start()
        except asyncio.CancelledError:
            logger.info("Feed cancelled — shutting down.")
        except Exception as e:
            logger.error(f"Feed crashed: {e}", exc_info=True)
            try:
                await self._telegram.send(f"💥 <b>Feed Crashed</b>\n<code>{e}</code>")
            except Exception:
                pass
            try:
                await self._whatsapp.send(f"💥 *Feed Crashed*\n```{e}```")
            except Exception:
                pass
            raise
        finally:
            await self.shutdown()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def _main() -> None:
    bot  = ShivaSniperBot()
    loop = asyncio.get_running_loop()

    def _handle_signal(sig_num: int) -> None:
        logger.info(f"Signal {sig_num} — graceful shutdown initiated...")
        for task in asyncio.all_tasks(loop):
            # Skip the main bot_run task — it handles shutdown itself via
            # CancelledError/finally, and cancelling it here races with the
            # shutdown() coroutine, dropping the WhatsApp stop notification.
            if task.get_name() != "bot_run":
                task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda sn=s: _handle_signal(sn))
        except NotImplementedError:
            pass  # Windows

    run_task = asyncio.create_task(bot.run(), name="bot_run")
    await run_task


if __name__ == "__main__":
    asyncio.run(_main())


# ── Backward-compat re-exports so old phase scripts keep working ───────────────
from orders.manager     import OrderManager, build_exchange          # noqa: E402,F401
from monitor.trail_loop import TrailMonitor                          # noqa: E402,F401
from indicators.engine  import IndicatorSnapshot, Signal, SignalType # noqa: E402,F401
from risk.calculator    import RiskLevels, TrailState                # noqa: E402,F401
from execution import ExecutionEngine, log_signal                    # noqa: E402,F401
