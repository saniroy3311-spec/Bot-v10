import time
import logging
from config import (
    PINE_MINTICK,
    TRAIL_OFFSET_FLOOR_MULT,
    TIME_EXIT_MINUTES,
    TRAIL_EXIT_FROM_DELTA_WS,
    TRAIL_FIRE_SL_ON_CANDLE_EXTREME
)

logger = logging.getLogger("trail_loop")

class TrailMonitor:
    def __init__(self, bot=None, order_mgr=None, **kwargs):
        """
        Manages high-cadence tracking using live price streams.
        Preserves microsecond processing safety loops across execution windows.
        """
        self.bot = bot
        self._active = False
        self._running = False
        self.entry_price = 0.0
        self.risk = None
        self.is_long = False
        self._offset = 0.0
        self._last_recal_ms = 0
        self.best_price = None
        self.trail_sl = None
        self.trail_armed = False
        self._entry_time_ms = 0
        self.trail_pts = 0.0
        self.trail_off = 0.0

    def start(self, entry_price: float, risk, is_long: bool, signal_bar_ohlc=None):
        """
        Initializes and launches the trailing stop monitor loop.
        """
        self.entry_price = entry_price
        self.risk = risk
        self.is_long = is_long
        self._active = True
        self.trail_armed = False
        self._entry_time_ms = int(time.time() * 1000)
        
        # Bug 10 Fix: Seed initialization clock at exact trade launch window
        self._last_recal_ms = int(time.time() * 1000) 
        
        # Bug 3/16 Fix: Dynamic extraction metrics anchoring to signal close
        atr = risk.atr if hasattr(risk, 'atr') else 250.0
        
        # Bug 16 Fix: Floor limit guard checking raw metric levels against noise
        self.trail_off = max(atr * 0.22 * PINE_MINTICK, atr * TRAIL_OFFSET_FLOOR_MULT)
        self.trail_pts = atr * 0.07  
        self.trail_sl = risk.sl
        self.best_price = None

        logger.info(
            f"[TRAIL] Started | entry={entry_price:.2f} sl={risk.sl:.2f} tp={risk.tp:.2f} "
            f"entry_atr={atr:.2f} is_long={is_long} | trail_pts={self.trail_pts:.2f} trail_off={self.trail_off:.2f}"
        )

        # Bug 6 Fix: Informational tracking only, isolated from baseline metrics calculation
        if signal_bar_ohlc:
            logger.info(
                f"[TRAIL] Signal bar OHLC (informational, not applied to trail) | "
                f"high={signal_bar_ohlc.get('high', 0):.2f} low={signal_bar_ohlc.get('low', 0):.2f} "
                f"close={signal_bar_ohlc.get('close', 0):.2f} atr={atr:.2f}"
            )

    def stop(self):
        """
        Safely halts state processing flags.
        """
        self._active = False
        logger.info("TrailMonitor stopped.")

    def _recalibrate_offset(self, binance_px: float, delta_px: float):
        """
        Calculates localized divergence parameters between backend sources.
        """
        # ─── SAME-CANDLE LOCK MECHANISM ─────────────────────────────────────
        # CRITICAL FIX: Freeze parameters instantly if an execution thread is active.
        # This completely stops 30-second mid-candle jumps from sliding the trail boundaries.
        if self._active:
            return
        # ────────────────────────────────────────────────────────────────────

        now_ms = int(time.time() * 1000)
        # Bug 14 Fix: Compact 20-second cooling block throttle validation
        if now_ms - self._last_recal_ms < 20000: 
            return

        old_offset = self._offset
        self._offset = binance_px - delta_px
        self._last_recal_ms = now_ms
        logger.info(
            f"[TRAIL] Offset recalibrated: {old_offset:+.2f} → {self._offset:+.2f} "
            f"(binance={binance_px:.2f} delta={delta_px:.2f})"
        )

    def push_delta_tick(self, price: float):
        """
        Bug 14 Fix: Processes native price ticks to bypass translation layers entirely.
        """
        if not self._active:
            return
        self._evaluate_trail(price, src="delta_tick")

    def on_price_tick(self, binance_price: float):
        """
        Bug 2 Fix: Processes live tick events immediately with no core throttling loops.
        """
        if not self._active:
            if hasattr(self.bot, 'last_delta_tick') and self.bot.last_delta_tick:
                self._recalibrate_offset(binance_price, self.bot.last_delta_tick)
            return

        # Translate coordinates utilizing frozen reference points
        translated_price = binance_price - self._offset
        self._evaluate_trail(translated_price, src="tick")

    def push_ws_candle(self, candle_high: float, candle_low: float, candle_close: float):
        """
        Validates interval boundaries.
        """
        if not self._active:
            return

        # Bug 17 Fix: Shifts tracking metrics exclusively from clear extreme updates
        if self.is_long:
            if self.best_price is None or candle_high > self.best_price:
                self.best_price = candle_high
                self._update_trailing_stop(src="ws_candle_extreme")
        else:
            if self.best_price is None or candle_low < self.best_price:
                self.best_price = candle_low
                self._update_trailing_stop(src="ws_candle_extreme")

        if TRAIL_FIRE_SL_ON_CANDLE_EXTREME:
            adverse_extreme = candle_low if self.is_long else candle_high
            self._check_stop_trigger(adverse_extreme, src="ws_candle_extreme")

    def _evaluate_trail(self, current_price: float, src: str):
        # Mandatory Wall Clock Safety Enforcer
        if TIME_EXIT_MINUTES > 0:
            now_ms = int(time.time() * 1000)
            if now_ms - self._entry_time_ms >= (TIME_EXIT_MINUTES * 60 * 1000):
                self._fire_exit(reason=f"Time exit ({TIME_EXIT_MINUTES}m)", price=current_price, src=src)
                return

        if not self.trail_armed:
            if self.is_long:
                if current_price >= self.entry_price + self.trail_pts:
                    self.best_price = current_price
                    self.trail_armed = True
                    self.trail_sl = self.best_price - self.trail_off
                    logger.info(
                        f"[TRAIL] Trail ARMED | price={current_price:.2f} act_price={self.entry_price + self.trail_pts:.2f} "
                        f"trail_sl={self.trail_sl:.2f} trail_pts={self.trail_pts:.2f} trail_off={self.trail_off:.2f}"
                    )
            else:
                if current_price <= self.entry_price - self.trail_pts:
                    self.best_price = current_price
                    self.trail_armed = True
                    self.trail_sl = self.best_price + self.trail_off
                    logger.info(
                        f"[TRAIL] Trail ARMED | price={current_price:.2f} act_price={self.entry_price - self.trail_pts:.2f} "
                        f"trail_sl={self.trail_sl:.2f} trail_pts={self.trail_pts:.2f} trail_off={self.trail_off:.2f}"
                    )
        else:
            old_sl = self.trail_sl
            updated = False
            if self.is_long:
                if current_price > self.best_price:
                    self.best_price = current_price
                    self.trail_sl = self.best_price - self.trail_off
                    updated = True
            else:
                if current_price < self.best_price:
                    self.best_price = current_price
                    self.trail_sl = self.best_price + self.trail_off
                    updated = True

            if updated and self.trail_sl != old_sl:
                logger.info(
                    f"[TRAIL] Trail SL: {old_sl:.2f} → {self.trail_sl:.2f} "
                    f"(stage 0 best={self.best_price:.2f} src={src})"
                )

        self._check_stop_trigger(current_price, src)

    def _update_trailing_stop(self, src: str):
        if not self.trail_armed or self.best_price is None:
            return
        old_sl = self.trail_sl
        if self.is_long:
            self.trail_sl = self.best_price - self.trail_off
        else:
            self.trail_sl = self.best_price + self.trail_off
        if self.trail_sl != old_sl:
            logger.info(
                f"[TRAIL] Trail SL: {old_sl:.2f} → {self.trail_sl:.2f} "
                f"(stage 0 best={self.best_price:.2f} src={src})"
            )

    def _check_stop_trigger(self, price: float, src: str):
        if not self._active:
            return
        
        if self.is_long:
            if price <= self.trail_sl:
                self._fire_exit(reason="Trail SL (stage 0)" if self.trail_armed else "Initial SL", price=self.trail_sl, src=src)
            elif hasattr(self.risk, 'tp') and price >= self.risk.tp:
                self._fire_exit(reason="Take Profit", price=self.risk.tp, src=src)
        else:
            if price >= self.trail_sl:
                self._fire_exit(reason="Trail SL (stage 0)" if self.trail_armed else "Initial SL", price=self.trail_sl, src=src)
            elif hasattr(self.risk, 'tp') and price <= self.risk.tp:
                self._fire_exit(reason="Take Profit", price=self.risk.tp, src=src)

    def _fire_exit(self, reason: str, price: float, src: str):
        self._active = False
        atr_val = self.risk.atr if self.risk and hasattr(self.risk, 'atr') else 0.0
        logger.info(f"[TRAIL] Exit fired: reason={reason} price={price:.2f} source={src} atr={atr_val:.2f}")
        if self.bot and hasattr(self.bot, 'handle_trail_exit'):
            import asyncio
            asyncio.create_task(self.bot.handle_trail_exit(price, reason, src))
