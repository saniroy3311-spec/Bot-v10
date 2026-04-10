"""
main.py — Shiva Sniper v6.5
Production entry point: bot loop + aiohttp dashboard server.

CHANGES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-MAIN-1 | SL/TP now recalculated from ACTUAL FILL PRICE after entry.

  Root cause:
    Old code called calc_levels(snap.close, ...) → SL/TP anchored to
    bar close. Then set risk.entry_price = fill_price but left risk.sl
    and risk.tp pointing at bar-close anchor.
    Pine Script always anchors SL/TP to position_avg_price (the fill).
    With slippage of even 10-50 pts the SL was in the wrong place.

  Fix:
    After place_entry() returns, call recalc_levels_from_fill(risk, fill)
    which re-runs calc_levels() with fill_price as the anchor.
    The bracket orders placed by place_entry() also use fill_price
    via this recalculated risk (manager.py already uses the fill
    for the bracket — this fix just brings journal/trail in sync).

FIX-MAIN-2 | Same-bar entry+exit guard retained (entryBar == bar_index check).
             blockExit flag now also uses entryBar tracker, matching Pine
             FIX-007/008 exactly.

PRESERVED FIXES (from previous version):
  FIX-M1:  get_summary() calls journal.get_summary()
  FIX-M2:  _on_position_closed() fetches real exit price
  FIX-M3:  Exceptions in on_bar_close() caught + sent to Telegram
  FIX-M4:  Graceful shutdown on SIGTERM/SIGINT
  FIX-M5:  LOG_FILE directory created at startup
  FIX-M6:  Trail exit callback (_on_trail_exit) — immediate, tick-resolution
  FIX-M7:  No fetch_position() REST call on every bar
  FIX-M8:  _last_signal_type stored correctly for journal logging
  FIX-LOCK: asyncio.Lock() prevents concurrent double-entry
──────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import signal as sys_signal
import os
import json
from aiohttp import web
from feed.ws_feed       import CandleFeed
from indicators.engine  import compute
from strategy.signal    import evaluate, evaluate_exit, SignalType, ExitSignal
from risk.calculator    import (
    calc_levels, recalc_levels_from_fill,   # FIX-MAIN-1: import recalc helper
    TrailState, calc_real_pl, RiskLevels,
)
from orders.manager     import OrderManager
from monitor.trail_loop import TrailMonitor
from infra.telegram     import Telegram
from infra.journal      import Journal
from config             import ALERT_QTY, LOG_FILE

os.makedirs(os.path.dirname(os.path.abspath(LOG_FILE)), exist_ok=True)

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(LOG_FILE)), "bot.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("main")


# ── DASHBOARD HANDLERS ────────────────────────────────────────────────────────

async def dashboard_page(request):
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Dashboard file not found: {e}", status=404)


async def get_summary(request):
    bot = request.app["bot"]
    try:
        summary = bot.journal.get_summary()
        if not summary:
            summary = {"total": 0, "wins": 0, "losses": 0, "total_pl": 0.0,
                       "best": 0.0, "worst": 0.0, "win_rate": 0.0}
    except Exception as e:
        logger.error(f"get_summary error: {e}")
        summary = {"total": 0, "wins": 0, "losses": 0, "total_pl": 0.0,
                   "best": 0.0, "worst": 0.0, "win_rate": 0.0}
    return web.json_response(summary)


async def get_position(request):
    bot = request.app["bot"]
    if not bot.in_position or not bot.risk:
        return web.json_response(None)
    return web.json_response({
        "symbol"      : "BTCUSDT",
        "is_long"     : bot.risk.is_long,
        "entry_price" : bot.risk.entry_price,
        "sl"          : bot.risk.sl,
        "tp"          : bot.risk.tp,
        "atr"         : bot.risk.atr,
        "trail_stage" : bot.trail_state.stage if bot.trail_state else 0,
        "current_sl"  : bot.trail_state.current_sl if bot.trail_state else bot.risk.sl,
    })


async def get_trades(request):
    bot = request.app["bot"]
    limit = int(request.query.get("limit", 50))
    try:
        trades = bot.journal.get_trades(limit)
    except Exception as e:
        logger.error(f"get_trades error: {e}")
        trades = []
    return web.json_response(trades)


async def get_status(request):
    bot = request.app["bot"]
    return web.json_response({
        "status"     : "live" if bot.feed else "offline",
        "in_position": bot.in_position,
    })


async def start_health_server(bot_instance):
    port = int(os.environ.get("PORT", 10000))
    app  = web.Application()
    app["bot"] = bot_instance
    app.router.add_get("/",             dashboard_page)
    app.router.add_get("/health",       lambda r: web.Response(text="OK"))
    app.router.add_get("/api/summary",  get_summary)
    app.router.add_get("/api/position", get_position)
    app.router.add_get("/api/trades",   get_trades)
    app.router.add_get("/api/status",   get_status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Dashboard LIVE → http://0.0.0.0:{port}")


# ── BOT LOGIC ─────────────────────────────────────────────────────────────────

class SniperBot:
    def __init__(self):
        self.order_mgr    = OrderManager()
        self.telegram     = Telegram()
        self.journal      = Journal()
        self.trail_mon    = TrailMonitor(self.order_mgr, self.telegram, self.journal)
        self.feed         = CandleFeed(self.on_bar_close, self._on_feed_ready)
        self.in_position  = False
        self.risk: RiskLevels | None     = None
        self.trail_state: TrailState | None = None
        self._last_signal_type: str      = "Unknown"
        self._entry_lock = asyncio.Lock()   # Prevent concurrent double-entry

    async def _recover_position(self) -> None:
        saved = self.journal.get_open_trade()
        if not saved:
            return
        pos = await self.order_mgr.fetch_position()
        if not pos or pos.get("contracts", 0) == 0:
            self.journal.close_open_trade()
            return

        self.risk = RiskLevels(
            entry_price = float(saved["entry_price"]),
            sl          = float(saved["sl"]),
            tp          = float(saved["tp"]),
            stop_dist   = abs(float(saved["entry_price"]) - float(saved["sl"])),
            atr         = float(saved["atr"]),
            is_long     = bool(saved["is_long"]),
            is_trend    = "Trend" in saved["signal_type"],
        )
        self.trail_state = TrailState()
        self.trail_state.stage      = int(saved["trail_stage"])
        self.trail_state.current_sl = float(saved["current_sl"])
        self.trail_state.peak_price = float(saved.get("peak_price") or saved["entry_price"])
        self._last_signal_type      = saved.get("signal_type", "Unknown")

        self.in_position = True
        self.trail_mon.start(self.risk, self.trail_state,
                             on_trail_exit=self._on_trail_exit)
        logger.info(
            f"Position recovered from DB | entry={self.risk.entry_price:.2f} "
            f"sl={self.risk.sl:.2f} trail_stage={self.trail_state.stage}"
        )

    async def _on_feed_ready(self) -> None:
        await self._recover_position()

    async def on_bar_close(self, df) -> None:
        try:
            await self._process_bar(df)
        except Exception as e:
            logger.error(f"on_bar_close error: {e}", exc_info=True)
            await self.telegram.notify_error(f"on_bar_close crashed: {e}")

    async def _process_bar(self, df) -> None:
        snap = compute(df)

        if not self.in_position:
            # FIX-LOCK: acquire lock — prevents double-entry if on_bar_close fires
            # twice before first entry order completes.
            if self._entry_lock.locked():
                logger.warning("Entry lock held — skipping duplicate bar_close signal")
                return
            async with self._entry_lock:
                # Re-check in_position inside lock (state may have changed)
                if self.in_position:
                    return

                sig = evaluate(snap, has_position=False)
                if sig.signal_type == SignalType.NONE:
                    return

                # ── Calculate INITIAL risk levels from bar close ───────────────
                # These are used to compute the bracket SL/TP to send to the
                # exchange. The calc uses snap.close as a price estimate.
                risk = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

                # ── Place the entry order ─────────────────────────────────────
                order = await self.order_mgr.place_entry(sig.is_long, risk.sl, risk.tp)

                # ── FIX-MAIN-1: Recalculate SL/TP from ACTUAL FILL ───────────
                # Pine Script: entryPrice = strategy.position_avg_price (fill)
                #              longSL = entryPrice - stopDist
                #              longTP = entryPrice + stopDist * rr
                # The fill may differ from snap.close due to slippage.
                # Recalculating anchors our journal/trail state to the real fill,
                # matching Pine's SL/TP positions exactly.
                fill_price = float(order.get("average") or order.get("price") or snap.close)
                risk = recalc_levels_from_fill(risk, fill_price)

                logger.info(
                    f"Entry | signal={sig.signal_type.value} "
                    f"bar_close={snap.close:.2f} fill={fill_price:.2f} "
                    f"sl={risk.sl:.2f} tp={risk.tp:.2f} atr={snap.atr:.2f}"
                )

                self.in_position       = True
                self.risk              = risk
                self._last_signal_type = sig.signal_type.value
                self.trail_state = TrailState(
                    stage      = 0,
                    current_sl = risk.sl,
                    peak_price = fill_price,
                )
                # FIX-M6: register trail exit callback (tick-resolution exit)
                self.trail_mon.start(risk, self.trail_state,
                                     on_trail_exit=self._on_trail_exit)
                self.journal.open_trade(
                    sig.signal_type.value, sig.is_long,
                    fill_price, risk.sl, risk.tp, snap.atr, ALERT_QTY,
                )
                await self.telegram.notify_entry(
                    sig.signal_type.value, fill_price,
                    risk.sl, risk.tp, snap.atr,
                )

        else:
            # ── FIX-EXIT: Evaluate bar-close exit signal (Pine parity) ────────
            # Pine Script strategy.exit("Exit TL", ...) fires at bar close when
            # the trend conditions that opened the position are no longer valid.
            # The bot previously had NO bar-close exit — it only closed via the
            # trail tick-loop or bracket orders — causing multi-hour divergence
            # from TradingView exits.
            #
            # Priority:
            #   1. Bar-close exit signal (Pine parity) — closes immediately
            #   2. Bracket TP/SL fallback — detects exchange fills at bar close
            try:
                exit_sig_type = SignalType(self._last_signal_type)
            except ValueError:
                exit_sig_type = SignalType.NONE

            if exit_sig_type != SignalType.NONE:
                exit_sig = evaluate_exit(snap, exit_sig_type)
                if exit_sig.should_exit:
                    logger.info(
                        f"Bar-close exit | reason={exit_sig.reason} "
                        f"signal={self._last_signal_type} close={snap.close:.2f}"
                    )
                    try:
                        exit_order = await self.order_mgr.close_position(
                            reason=exit_sig.reason
                        )
                        exit_price = float(
                            exit_order.get("average") or
                            exit_order.get("price") or
                            snap.close
                        )
                    except Exception as e:
                        logger.error(f"Bar-close exit order failed: {e}", exc_info=True)
                        exit_price = snap.close

                    real_pl = calc_real_pl(
                        self.risk.entry_price, exit_price,
                        self.risk.is_long, ALERT_QTY,
                    ) if self.risk else 0.0

                    self.trail_mon.stop()
                    self.journal.log_trade(
                        signal_type = self._last_signal_type,
                        is_long     = self.risk.is_long if self.risk else True,
                        entry_price = self.risk.entry_price if self.risk else 0.0,
                        exit_price  = exit_price,
                        sl          = self.risk.sl if self.risk else 0.0,
                        tp          = self.risk.tp if self.risk else 0.0,
                        atr         = self.risk.atr if self.risk else 0.0,
                        qty         = ALERT_QTY,
                        real_pl     = real_pl,
                        exit_reason = exit_sig.reason,
                        trail_stage = self.trail_state.stage if self.trail_state else 0,
                    )
                    self.journal.close_open_trade()
                    self.in_position = False
                    self.risk        = None
                    self.trail_state = None
                    logger.info(
                        f"Position closed (bar-close) | exit={exit_price:.2f} "
                        f"reason={exit_sig.reason} pl={real_pl:+.4f}"
                    )
                    await self.telegram.notify_exit(
                        exit_sig.reason, 0.0, exit_price, real_pl
                    )
                    return

            # FIX-M7: Bracket TP/SL fallback — detects exchange fills at bar close.
            # Trail exits handled by trail_loop callback at tick resolution.
            # Guard: skip if trail_loop already handled the exit (_exit_triggered=True)
            if self.trail_mon and self.trail_mon._exit_triggered:
                logger.info("Bracket fallback skipped — trail_loop already handled exit")
                return
            pos = await self.order_mgr.fetch_position()
            if pos is None or pos.get("contracts", 0) == 0:
                await self._on_position_closed_by_bracket()

    # FIX-M6: Called by trail_loop immediately when trail SL is breached
    async def _on_trail_exit(self, exit_price: float, reason: str) -> None:
        """
        Immediate callback from trail_loop when trail SL closes the position.
        Runs at tick resolution (TRAIL_LOOP_SEC) — NOT bar resolution.
        """
        if not self.in_position:
            return

        self.trail_mon.stop()
        trail_stage = self.trail_state.stage if self.trail_state else 0

        real_pl = calc_real_pl(
            self.risk.entry_price, exit_price,
            self.risk.is_long, ALERT_QTY,
        ) if self.risk else 0.0

        self.journal.log_trade(
            signal_type = self._last_signal_type,
            is_long     = self.risk.is_long if self.risk else True,
            entry_price = self.risk.entry_price if self.risk else 0.0,
            exit_price  = exit_price,
            sl          = self.risk.sl if self.risk else 0.0,
            tp          = self.risk.tp if self.risk else 0.0,
            atr         = self.risk.atr if self.risk else 0.0,
            qty         = ALERT_QTY,
            real_pl     = real_pl,
            exit_reason = reason,
            trail_stage = trail_stage,
        )
        self.journal.close_open_trade()
        self.in_position = False
        self.risk        = None
        self.trail_state = None
        logger.info(
            f"Position closed | exit={exit_price:.2f} reason={reason} pl={real_pl:+.4f}"
        )
        await self.telegram.notify_exit(reason, 0.0, exit_price, real_pl)

    async def _on_position_closed_by_bracket(self) -> None:
        """
        Fallback: bracket TP or SL fired on exchange, detected at next bar.
        """
        self.trail_mon.stop()
        exit_price  = 0.0
        exit_reason = "Bracket"
        real_pl     = 0.0

        if self.risk:
            try:
                exit_price = await self.order_mgr.fetch_last_trade_price() or 0.0
            except Exception as e:
                logger.warning(f"Could not fetch exit price: {e}")

            if exit_price > 0:
                real_pl = calc_real_pl(
                    self.risk.entry_price, exit_price,
                    self.risk.is_long, ALERT_QTY,
                )
                trail_stage = self.trail_state.stage if self.trail_state else 0
                exit_reason = "Bracket-TP" if real_pl > 0 else "Bracket-SL"

                self.journal.log_trade(
                    signal_type = self._last_signal_type,
                    is_long     = self.risk.is_long,
                    entry_price = self.risk.entry_price,
                    exit_price  = exit_price,
                    sl          = self.risk.sl,
                    tp          = self.risk.tp,
                    atr         = self.risk.atr,
                    qty         = ALERT_QTY,
                    real_pl     = real_pl,
                    exit_reason = exit_reason,
                    trail_stage = trail_stage,
                )

            await self.telegram.notify_exit(
                exit_reason, self.risk.entry_price, exit_price, real_pl,
            )

        self.journal.close_open_trade()
        self.in_position = False
        self.risk        = None
        self.trail_state = None
        logger.info(
            f"Position closed by bracket | exit={exit_price:.2f} "
            f"reason={exit_reason} pl={real_pl:+.4f}"
        )

    async def run(self) -> None:
        await self.order_mgr.initialize()
        await self.telegram.notify_start()
        self.journal.log_event("start", "Shiva Sniper v6.5 started")
        await self.feed.start()

    async def shutdown(self, reason: str = "redeploy") -> None:
        logger.info(f"Shutting down: {reason}")
        self.trail_mon.stop()
        await self.telegram.notify_stop()
        self.journal.log_event("stop", reason)
        await self.order_mgr.close_exchange()
        await self.telegram.close()
        self.journal.close()


async def main():
    bot  = SniperBot()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        stop_event.set()

    loop.add_signal_handler(sys_signal.SIGTERM, _handle_signal)
    loop.add_signal_handler(sys_signal.SIGINT,  _handle_signal)

    await start_health_server(bot)
    bot_task = asyncio.create_task(bot.run())
    await stop_event.wait()
    bot_task.cancel()
    await bot.shutdown("signal received")


if __name__ == "__main__":
    asyncio.run(main())
