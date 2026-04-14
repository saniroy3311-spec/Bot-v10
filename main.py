"""
main.py — Shiva Sniper v6.5
Production entry point: bot loop + aiohttp dashboard server.

FIXES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────
FIX-MAIN-3 | Pass snap.atr into trail_mon.on_bar_close() (Bug 2 fix)
  Pine recalculates atr every bar. The trail monitor now receives the
  current bar's ATR so all trail offset calculations stay in sync.

FIX-MAIN-4 | Same-bar re-entry guard (Bug 4 fix)
  After _on_trail_exit() fires and clears in_position, main can
  immediately re-evaluate entries on the SAME bar index (async gap).
  Pine's noPosition is evaluated once per bar — once you exit on a bar,
  no new entry is possible until the NEXT bar closes.
  Fix: track _last_exit_bar_ts. If on_bar_close fires with the same
  bar timestamp as the exit bar, skip entry evaluation entirely.

PRESERVED FIXES:
  FIX-MAIN-1: recalc_levels_from_fill (SL/TP anchored to fill price)
  FIX-MAIN-2: same-bar entry+exit guard (entryBar == bar_index)
  FIX-M1..M8: all prior fixes
──────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import signal as sys_signal
import os
import json
from aiohttp import web
from feed.ws_feed       import CandleFeed
from indicators.engine  import compute
from strategy.signal    import evaluate, SignalType
from risk.calculator    import (
    calc_levels, recalc_levels_from_fill,
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
        self._entry_lock = asyncio.Lock()

        # FIX-MAIN-4: track the bar timestamp when last exit fired
        # If on_bar_close() fires with this same timestamp, skip entry eval.
        self._last_exit_bar_ts: int | None = None

        # Track current bar timestamp so _on_trail_exit can read it
        self._current_bar_ts: int = 0

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

        # Store current bar timestamp for same-bar exit guard (FIX-MAIN-4)
        self._current_bar_ts = snap.timestamp

        # FIX-MAIN-3: Pass snap.atr (current bar ATR) to trail monitor.
        # Pine recalculates atr every bar — trail offsets must follow.
        if self.in_position:
            self.trail_mon.on_bar_close(snap.close, snap.high, snap.low, snap.atr)

        if not self.in_position:
            if self._entry_lock.locked():
                logger.warning("Entry lock held — skipping duplicate bar_close signal")
                return
            async with self._entry_lock:
                if self.in_position:
                    return

                # FIX-MAIN-4: Skip entry if the exit just happened on this bar.
                # Pine's noPosition gate is evaluated once at bar open —
                # once you're out on bar N, no re-entry until bar N+1.
                if self._last_exit_bar_ts is not None and \
                   self._last_exit_bar_ts == self._current_bar_ts:
                    logger.info(
                        f"Same-bar re-entry blocked | bar_ts={self._current_bar_ts} "
                        f"exit_ts={self._last_exit_bar_ts} (Pine parity)"
                    )
                    return

                sig = evaluate(snap, has_position=False)

                logger.info(
                    f"BAR | close={snap.close:.2f} "
                    f"adx={snap.adx:.2f} "
                    f"regime={'TREND' if snap.trend_regime else 'RANGE' if snap.range_regime else 'NONE'} "
                    f"ema_fast={snap.ema_fast:.2f} ema_trend={snap.ema_trend:.2f} "
                    f"dip={snap.dip:.2f} dim={snap.dim:.2f} rsi={snap.rsi:.2f} "
                    f"atr={snap.atr:.2f} atr_sma={snap.atr_sma:.2f} "
                    f"vol={snap.volume:.0f} vol_sma={snap.vol_sma:.0f} "
                    f"prev_high={snap.prev_high:.2f} prev_low={snap.prev_low:.2f} "
                    f"| atr_ok={snap.atr_ok} vol_ok={snap.vol_ok} body_ok={snap.body_ok} "
                    f"=> signal={sig.signal_type.value}"
                )

                if sig.signal_type == SignalType.NONE:
                    return

                risk = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)
                order = await self.order_mgr.place_entry(sig.is_long, risk.sl, risk.tp)

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
            if self.trail_mon and self.trail_mon._exit_triggered:
                logger.info("Bracket fallback skipped — trail_loop already handled exit")
                return
            pos = await self.order_mgr.fetch_position()
            if pos is None or pos.get("contracts", 0) == 0:
                await self._on_position_closed_by_bracket()

    async def _on_trail_exit(self, exit_price: float, reason: str) -> None:
        if not self.in_position:
            return

        # FIX-MAIN-4: record which bar this exit happened on
        self._last_exit_bar_ts = self._current_bar_ts

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
        _entry_price = self.risk.entry_price if self.risk else 0.0
        _is_long     = self.risk.is_long     if self.risk else True

        self.journal.close_open_trade()
        self.in_position = False
        self.risk        = None
        self.trail_state = None
        logger.info(
            f"Position closed | exit={exit_price:.2f} reason={reason} pl={real_pl:+.4f}"
        )
        await self.telegram.notify_exit(reason, _entry_price, exit_price, real_pl, _is_long)

    async def _on_position_closed_by_bracket(self) -> None:
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

                # FIX-MAIN-5: Correctly label TP exits caught by the bracket
                # fallback (position already closed on exchange before _on_tick
                # could detect it). If trail was active and PL is positive,
                # this is a Trail TP — not a generic Bracket-TP.
                # Pine: exit at TP = "Target Profit", whether caught by trail
                # loop or exchange-side bracket.
                if real_pl > 0:
                    if trail_stage > 0:
                        exit_reason = f"Trail S{trail_stage} TP"
                    else:
                        exit_reason = "Target Profit"
                else:
                    exit_reason = "Bracket-SL"

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
                self.risk.is_long,
            )

        # FIX-MAIN-4: record exit bar for same-bar guard
        self._last_exit_bar_ts = self._current_bar_ts

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
