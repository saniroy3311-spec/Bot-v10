"""
main.py — Shiva Sniper v6.5
Production entry point: bot loop + aiohttp dashboard server.

FIXES vs original:
  FIX-M1: get_summary() now calls journal.get_summary() instead of returning zeros.
  FIX-M2: _on_position_closed() fetches real exit price, logs trade to journal,
           and sends correct Telegram notification with actual P/L.
  FIX-M3: Unhandled exceptions in on_bar_close() are caught and sent to Telegram.
  FIX-M4: Graceful shutdown on SIGTERM/SIGINT via asyncio signal handler.
  FIX-M5: LOG_FILE directory is created at startup if missing.
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
from risk.calculator    import calc_levels, TrailState, calc_real_pl, RiskLevels
from orders.manager     import OrderManager
from monitor.trail_loop import TrailMonitor
from infra.telegram     import Telegram
from infra.journal      import Journal
from config             import ALERT_QTY, LOG_FILE

# ── Ensure journal directory exists ──────────────────────────────────────────
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


# ── DASHBOARD & API HANDLERS ──────────────────────────────────────────────────

async def dashboard_page(request):
    """Serves the dashboard.html file to the browser."""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except Exception as e:
        logger.error(f"Dashboard file not found: {e}")
        return web.Response(text=f"Dashboard file not found: {e}", status=404)


async def get_summary(request):
    """Feeds the 'Performance Overview' section of the dashboard."""
    bot = request.app["bot"]
    # FIX-M1: call journal.get_summary() — was returning hardcoded zeros before
    try:
        summary = bot.journal.get_summary()
        if not summary:
            summary = {
                "total": 0, "wins": 0, "losses": 0, "total_pl": 0.0,
                "best": 0.0, "worst": 0.0, "win_rate": 0.0,
            }
    except Exception as e:
        logger.error(f"get_summary error: {e}")
        summary = {
            "total": 0, "wins": 0, "losses": 0, "total_pl": 0.0,
            "best": 0.0, "worst": 0.0, "win_rate": 0.0,
        }
    return web.json_response(summary)


async def get_position(request):
    """Feeds the 'Open Position' and 'Trail Stage' section of the dashboard."""
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
    """Feeds the trade history table on the dashboard."""
    bot = request.app["bot"]
    limit = int(request.query.get("limit", 50))
    try:
        trades = bot.journal.get_trades(limit)
    except Exception as e:
        logger.error(f"get_trades error: {e}")
        trades = []
    return web.json_response(trades)


async def get_status(request):
    """Feeds the LIVE/OFFLINE badge on the dashboard."""
    bot = request.app["bot"]
    return web.json_response({
        "status"     : "live" if bot.feed else "offline",
        "in_position": bot.in_position,
    })


async def start_health_server(bot_instance):
    port = int(os.environ.get("PORT", 10000))
    app  = web.Application()
    app["bot"] = bot_instance

    app.router.add_get("/",            dashboard_page)
    app.router.add_get("/health",      lambda r: web.Response(text="OK"))
    app.router.add_get("/api/summary", get_summary)
    app.router.add_get("/api/position",get_position)
    app.router.add_get("/api/trades",  get_trades)
    app.router.add_get("/api/status",  get_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Dashboard LIVE → http://0.0.0.0:{port}")


# ── BOT LOGIC ─────────────────────────────────────────────────────────────────

class SniperBot:
    def __init__(self):
        self.order_mgr   = OrderManager()
        self.telegram    = Telegram()
        self.journal     = Journal()
        self.trail_mon   = TrailMonitor(self.order_mgr, self.telegram, self.journal)
        self.feed        = CandleFeed(self.on_bar_close, self._on_feed_ready)
        self.in_position = False
        self.signal_type = SignalType.NONE
        self.risk: RiskLevels | None     = None
        self.trail_state: TrailState | None = None

    async def _recover_position(self) -> None:
        saved = self.journal.get_open_trade()
        if not saved:
            return
        pos = await self.order_mgr.fetch_position()
        if not pos or pos.get("contracts", 0) == 0:
            self.journal.close_open_trade()
            return

        self.risk = RiskLevels(
            entry_price=float(saved["entry_price"]),
            sl=float(saved["sl"]),
            tp=float(saved["tp"]),
            stop_dist=abs(float(saved["entry_price"]) - float(saved["sl"])),
            atr=float(saved["atr"]),
            is_long=bool(saved["is_long"]),
            is_trend="Trend" in saved["signal_type"],
        )
        self.trail_state = TrailState()
        self.trail_state.stage      = int(saved["trail_stage"])
        self.trail_state.current_sl = float(saved["current_sl"])
        self.trail_state.peak_price = float(saved.get("peak_price") or saved["entry_price"])

        self.in_position = True
        self.trail_mon.start(self.risk, self.trail_state)
        logger.info(
            f"Position recovered from DB | entry={self.risk.entry_price:.2f} "
            f"sl={self.risk.sl:.2f} trail_stage={self.trail_state.stage}"
        )

    async def _on_feed_ready(self) -> None:
        await self._recover_position()

    async def on_bar_close(self, df) -> None:
        # FIX-M3: wrap in try/except so one bad bar doesn't crash the bot
        try:
            await self._process_bar(df)
        except Exception as e:
            logger.error(f"on_bar_close error: {e}", exc_info=True)
            await self.telegram.notify_error(f"on_bar_close crashed: {e}")

    async def _process_bar(self, df) -> None:
        snap = compute(df)

        if not self.in_position:
            sig = evaluate(snap, has_position=False)
            if sig.signal_type == SignalType.NONE:
                return

            risk = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)
            order = await self.order_mgr.place_entry(sig.is_long, risk.sl, risk.tp)

            self.in_position = True
            self.risk        = risk
            self.trail_state = TrailState(
                stage=0,
                current_sl=risk.sl,
                peak_price=risk.entry_price,
            )
            self.trail_mon.start(risk, self.trail_state)
            self.journal.open_trade(
                sig.signal_type.value, sig.is_long,
                risk.entry_price, risk.sl, risk.tp, snap.atr, ALERT_QTY,
            )
            await self.telegram.notify_entry(
                sig.signal_type.value, risk.entry_price,
                risk.sl, risk.tp, snap.atr,
            )
        else:
            pos = await self.order_mgr.fetch_position()
            if pos is None or pos.get("contracts", 0) == 0:
                await self._on_position_closed()

    async def _on_position_closed(self) -> None:
        """
        FIX-M2: fetch real exit price, compute P/L, log trade, send full Telegram alert.
        Original had placeholder zeros for entry/exit/pl.
        """
        self.trail_mon.stop()

        # Determine exit details
        exit_price  = 0.0
        real_pl     = 0.0
        exit_reason = "Unknown"

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
                # Classify exit reason
                trail_stage = self.trail_state.stage if self.trail_state else 0
                if trail_stage >= 3:
                    exit_reason = f"Trail S{trail_stage}"
                elif self.trail_state and self.trail_state.be_done:
                    exit_reason = "Breakeven"
                elif real_pl > 0:
                    exit_reason = "TP"
                else:
                    exit_reason = "SL"

                # Log completed trade
                self.journal.log_trade(
                    signal_type ="Unknown" if not hasattr(self, "_last_signal_type") else self._last_signal_type,
                    is_long     =self.risk.is_long,
                    entry_price =self.risk.entry_price,
                    exit_price  =exit_price,
                    sl          =self.risk.sl,
                    tp          =self.risk.tp,
                    atr         =self.risk.atr,
                    qty         =ALERT_QTY,
                    real_pl     =real_pl,
                    exit_reason =exit_reason,
                    trail_stage =trail_stage,
                )

            await self.telegram.notify_exit(
                exit_reason,
                self.risk.entry_price,
                exit_price,
                real_pl,
            )

        self.journal.close_open_trade()
        self.in_position = False
        self.risk        = None
        self.trail_state = None
        logger.info(
            f"Position closed | exit={exit_price:.2f} reason={exit_reason} pl={real_pl:+.2f}"
        )

    async def run(self) -> None:
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

    # FIX-M4: graceful shutdown on SIGTERM/SIGINT
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
