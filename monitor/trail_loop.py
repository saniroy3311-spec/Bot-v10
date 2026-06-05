"""
monitor/trail_loop.py — Shiva Sniper v10 trailing-stop engine
================================================================
INTRABAR STAGE UPGRADE MODIFICATION

WHAT CHANGED vs PREVIOUS VERSION:
─────────────────────────────────
1. Stage Upgrades and Breakeven now execute tick-by-tick (intrabar) inside
   the `_evaluate` function instead of waiting for `on_bar_close`.
2. This intentionally breaks strict Pine Parity to allow the bot to secure
   profits faster during high-volatility, single-candle spikes.
3. The `on_bar_close` function has been cleaned up and now only handles
   ATR updates and the safety-net exit catch.
"""

import time
import asyncio
import logging

from config import (
    PINE_MINTICK,
    TRAIL_OFFSET_FLOOR_MULT,
    TIME_EXIT_MINUTES,
    TRAIL_FIRE_SL_ON_CANDLE_EXTREME,
    TRAIL_STAGES,
    BE_MULT,
    MAX_SL_MULT,
    MAX_SL_POINTS,
    SL_FIRE_VIA_BRACKET,
    TRAIL_SL_PRE_FIRE_BUFFER,
    SL_CONFIRM_MS,
    BAR_CLOSE_SL_EVAL,
)

logger = logging.getLogger("trail_loop")

_SL_REASONS = ("Initial SL", "Trail SL", "Max SL")


class TrailMonitor:
    # ──────────────────────────────────────────────────────────────────────
    # Construction
    # ──────────────────────────────────────────────────────────────────────
    def __init__(self, order_mgr=None, telegram=None, journal=None, **kwargs):
        self._order_mgr = order_mgr
        self._telegram  = telegram
        self._journal   = journal

        self._running    = False
        self._exit_fired = False

        self._risk          = None          
        self._state         = None          
        self._on_trail_exit = None          
        self.is_long        = False
        self.entry_price    = 0.0
        self.atr            = 0.0           
        self.tp             = 0.0
        self._entry_time_ms = 0

        self.best_price   = None            
        self.trail_armed  = False
        self.trail_sl     = None
        self.be_done      = False
        self.max_sl_fired = False

        self._offset          = 0.0
        self._offset_locked   = False
        self._last_delta_seen = None

        self._sl_breach_start_ms = 0
        self._sl_breach_active   = False

        self._bar_true_high = None   
        self._bar_true_low  = None   

    # ──────────────────────────────────────────────────────────────────────
    # Start / stop
    # ──────────────────────────────────────────────────────────────────────
    def start(
        self,
        risk_levels,
        trail_state,
        entry_bar_time_ms,
        on_trail_exit,
        signal_bar_high=None,
        signal_bar_low=None,
        signal_bar_open=None,
        signal_bar_close=None,
        entry_wall_ms=None,
    ):
        self._risk          = risk_levels
        self._state         = trail_state
        self._on_trail_exit = on_trail_exit

        self.is_long     = bool(risk_levels.is_long)
        self.entry_price = float(risk_levels.entry_price)
        self.atr         = float(risk_levels.atr) if getattr(risk_levels, "atr", 0) else 250.0
        self.tp          = float(getattr(risk_levels, "tp", 0.0) or 0.0)

        if trail_state is not None:
            self.trail_sl     = float(trail_state.current_sl) if trail_state.current_sl else float(risk_levels.sl)
            self.best_price   = float(trail_state.peak_price) if trail_state.peak_price else self.entry_price
            self.be_done      = bool(getattr(trail_state, "be_done", False))
            self.max_sl_fired = bool(getattr(trail_state, "max_sl_fired", False))
            self.trail_armed  = int(getattr(trail_state, "stage", 0)) > 0
        else:
            t1_pts_dist = float(self.atr) * TRAIL_STAGES[0][1] * PINE_MINTICK
            self.trail_sl = (self.entry_price + t1_pts_dist) if not self.is_long else (self.entry_price - t1_pts_dist)
            self.best_price   = self.entry_price
            self.be_done      = False
            self.max_sl_fired = False
            self.trail_armed  = False

        self._entry_time_ms = int(entry_wall_ms or entry_bar_time_ms or time.time() * 1000)

        self._offset          = 0.0
        self._offset_locked   = False
        self._last_delta_seen = None

        self._sl_breach_start_ms = 0
        self._sl_breach_active   = False

        self._bar_true_high = None
        self._bar_true_low  = None

        self._exit_fired = False
        self._running    = True

        stage = int(getattr(trail_state, "stage", 0)) if trail_state else 0
        logger.info(
            f"[TRAIL] Started | entry={self.entry_price:.2f} sl={self.trail_sl:.2f} "
            f"tp={self.tp:.2f} entry_atr={self.atr:.2f} is_long={self.is_long} "
            f"stage={stage} armed={self.trail_armed}"
        )

    def stop(self):
        self._running = False
        logger.info("TrailMonitor stopped.")

    def set_entry_bar_boundary(self, next_bar_open_ms: int):
        return

    # ──────────────────────────────────────────────────────────────────────
    # Stage / offset helpers
    # ──────────────────────────────────────────────────────────────────────
    def _favorable_profit(self, ref_price):
        if self.is_long:
            return ref_price - self.entry_price
        return self.entry_price - ref_price

    def _stage_offset(self):
        stage = int(getattr(self._state, "stage", 0)) if self._state else 0
        if stage < 1:
            off_mult = TRAIL_STAGES[0][2]
        else:
            off_mult = TRAIL_STAGES[min(stage, len(TRAIL_STAGES)) - 1][2]
        raw   = self.atr * off_mult * PINE_MINTICK
        if TRAIL_OFFSET_FLOOR_MULT > 0.0:
            floor = self.atr * TRAIL_OFFSET_FLOOR_MULT
            return max(raw, floor)
        return raw

    def _recompute_trail_sl(self, src):
        if not self.trail_armed or self.best_price is None:
            return
        offset = self._stage_offset()
        if self.is_long:
            candidate = self.best_price - offset
            new_sl    = max(self.trail_sl, candidate) if self.trail_sl is not None else candidate
        else:
            candidate = self.best_price + offset
            new_sl    = min(self.trail_sl, candidate) if self.trail_sl is not None else candidate
        if new_sl != self.trail_sl:
            old = self.trail_sl
            self.trail_sl = new_sl
            stage = int(getattr(self._state, "stage", 0)) if self._state else 0
            logger.info(
                f"[TRAIL] SL: {old:.2f}→{self.trail_sl:.2f} | "
                f"stage={stage} best={self.best_price:.2f} off={offset:.2f} "
                f"atr={self.atr:.2f} entry={self.entry_price:.2f} src={src}"
            )
        self._sync_state()

    def _sync_state(self):
        if self._state is None:
            return
        if self.trail_sl is not None:
            self._state.current_sl = float(self.trail_sl)
        if self.best_price is not None:
            self._state.peak_price = float(self.best_price)
        self._state.be_done      = self.be_done
        self._state.max_sl_fired = self.max_sl_fired

    def _update_best(self, price):
        if self.best_price is None:
            self.best_price = price
            return True
        if self.is_long and price > self.best_price:
            self.best_price = price
            return True
        if (not self.is_long) and price < self.best_price:
            self.best_price = price
            return True
        return False

    # ──────────────────────────────────────────────────────────────────────
    # Bar close — ATR refresh & Safety Net (SYNC)
    # ──────────────────────────────────────────────────────────────────────
    def on_bar_close(self, bar_close, bar_high, bar_low, bar_open=None, current_atr=None):
        if not self._running or self._state is None:
            return

        if current_atr and current_atr > 0:
            self.atr = float(current_atr)

        self._update_best(bar_high if self.is_long else bar_low)
        self._recompute_trail_sl(src="bar_close")
        self._sync_state()

        stage = int(getattr(self._state, "stage", 0))
        logger.info(
            f"[TRAIL] Bar close | best={self.best_price:.2f} sl={self.trail_sl:.2f} "
            f"stage={stage} armed={self.trail_armed} live_atr={(current_atr or 0):.2f} "
            f"true_high={self._bar_true_high} true_low={self._bar_true_low}"
        )

        if not self._exit_fired and self.trail_armed:
            true_high = self._bar_true_high if self._bar_true_high is not None else bar_high
            true_low  = self._bar_true_low  if self._bar_true_low  is not None else bar_low

            trail_crossed = False
            if self.is_long:
                trail_crossed = true_low  <= self.trail_sl
            else:
                trail_crossed = true_high >= self.trail_sl

            if trail_crossed:
                logger.info(
                    f"[TRAIL] Safety-net Trail SL at bar close | "
                    f"true_high={true_high:.2f} true_low={true_low:.2f} "
                    f"trail_sl={self.trail_sl:.2f}"
                )
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._fire_exit("Trail SL", self.trail_sl, "bar_close_safety"))
                except RuntimeError:
                    pass

        self._bar_true_high = None
        self._bar_true_low  = None

    # ──────────────────────────────────────────────────────────────────────
    # Live ticks — Intrabar Logic & Exit firing (ASYNC)
    # ──────────────────────────────────────────────────────────────────────
    async def on_price_tick(self, price, source="binance"):
        if not self._running or self._exit_fired:
            return

        if source == "delta":
            self._last_delta_seen = float(price)
            px = float(price)
        else:
            if not self._offset_locked and self._last_delta_seen is not None:
                self._offset        = float(price) - float(self._last_delta_seen)
                self._offset_locked = True
                logger.info(f"[TRAIL] Offset locked: {self._offset:+.2f} "
                            f"(binance={price:.2f} delta={self._last_delta_seen:.2f})")
            px = float(price) - self._offset

        await self._evaluate(px, source)

    async def push_delta_tick(self, price):
        self._last_delta_seen = float(price)
        if not self._running or self._exit_fired:
            return
        px = float(price)
        if self._bar_true_high is None or px > self._bar_true_high:
            self._bar_true_high = px
        if self._bar_true_low is None or px < self._bar_true_low:
            self._bar_true_low = px
        await self._evaluate(px, "delta")

    async def _evaluate(self, price, src):
        if TIME_EXIT_MINUTES > 0:
            elapsed_ms = int(time.time() * 1000) - self._entry_time_ms
            if elapsed_ms >= TIME_EXIT_MINUTES * 60_000:
                await self._fire_exit(f"Time exit ({TIME_EXIT_MINUTES}m)", price, src)
                return

        if not self.trail_armed:
            if self._favorable_profit(price) > 0:
                self._update_best(price)
                self.trail_armed = True
                if self._state is not None and self._state.stage < 1:
                    self._state.stage = 1
                self._recompute_trail_sl(src=src)
                logger.info(
                    f"[TRAIL] Trail ARMED | price={price:.2f} "
                    f"trail_sl={self.trail_sl:.2f}"
                )
        else:
            best_moved = self._update_best(price)
            upgraded = False
            
            # ── INTRABAR STAGE UPGRADES ──
            if self._state is not None:
                profit = self._favorable_profit(self.best_price)
                stage = int(getattr(self._state, "stage", 0))
                
                while stage < len(TRAIL_STAGES) and profit >= self.atr * TRAIL_STAGES[stage][0]:
                    stage += 1
                    upgraded = True
                    
                if upgraded:
                    self._state.stage = stage
                    logger.info(
                        f"[TRAIL] Intrabar Stage → {stage} | profit={profit:.2f} "
                        f"(trigger={self.atr * TRAIL_STAGES[stage - 1][0]:.2f} atr={self.atr:.2f})"
                    )

                # ── INTRABAR BREAKEVEN ──
                if not self.be_done and profit >= self.atr * BE_MULT:
                    self.be_done = True
                    if self.is_long:
                        self.trail_sl = max(self.trail_sl or self.entry_price, self.entry_price)
                    else:
                        self.trail_sl = min(self.trail_sl or self.entry_price, self.entry_price)
                    logger.info(f"[TRAIL] Intrabar Breakeven armed | sl→{self.trail_sl:.2f} profit={profit:.2f}")
                    upgraded = True

            # If the peak moved OR the stage/BE upgraded, recalculate the SL
            if best_moved or upgraded:
                self._recompute_trail_sl(src=src)

        if not self.max_sl_fired:
            adverse = (self.entry_price - price) if self.is_long else (price - self.entry_price)
            max_dist = min(self.atr * MAX_SL_MULT, MAX_SL_POINTS)
            if adverse >= max_dist:
                self.max_sl_fired = True
                self._sync_state()
                await self._fire_exit("Max SL", price, src)
                return

        buf = TRAIL_SL_PRE_FIRE_BUFFER

        if self.is_long:
            sl_breached = self.trail_sl is not None and price <= self.trail_sl + buf
            tp_breached = self.tp and price >= self.tp
        else:
            sl_breached = self.trail_sl is not None and price >= self.trail_sl - buf
            tp_breached = self.tp and price <= self.tp

        if tp_breached:
            self._sl_breach_active = False
            self._sl_breach_start_ms = 0
            await self._fire_exit("Take Profit", self.tp, src)
            return

        if sl_breached:
            reason = "Trail SL" if self.trail_armed else "Initial SL"

            if self.trail_armed or SL_CONFIRM_MS <= 0:
                self._sl_breach_active = False
                self._sl_breach_start_ms = 0
                await self._fire_exit(reason, self.trail_sl, src)
                return

            now_ms = int(time.time() * 1000)
            if not self._sl_breach_active:
                self._sl_breach_active = True
                self._sl_breach_start_ms = now_ms
                logger.info(
                    f"[TRAIL] SL breach started (confirming) | price={price:.2f} "
                    f"sl={self.trail_sl:.2f} confirm_ms={SL_CONFIRM_MS}"
                )
                return

            elapsed = now_ms - self._sl_breach_start_ms
            if elapsed >= SL_CONFIRM_MS:
                logger.info(
                    f"[TRAIL] SL breach CONFIRMED after {elapsed}ms | "
                    f"price={price:.2f} sl={self.trail_sl:.2f}"
                )
                self._sl_breach_active = False
                self._sl_breach_start_ms = 0
                await self._fire_exit(reason, self.trail_sl, src)
        else:
            if self._sl_breach_active:
                logger.info(
                    f"[TRAIL] SL breach RESET (price recovered) | price={price:.2f} "
                    f"sl={self.trail_sl:.2f}"
                )
                self._sl_breach_active = False
                self._sl_breach_start_ms = 0

    # ──────────────────────────────────────────────────────────────────────
    # WS candle — favourable peak only (SYNC)
    # ──────────────────────────────────────────────────────────────────────
    def push_ws_candle(self, high, low, source="binance", close=None, **kwargs):
        if not self._running or self._exit_fired:
            return

        favorable = high if self.is_long else low
        if source != "delta":
            favorable = favorable - self._offset

        if self._update_best(favorable):
            self._recompute_trail_sl(src="ws_candle")

        if TRAIL_FIRE_SL_ON_CANDLE_EXTREME:
            adverse = low if self.is_long else high
            if source != "delta":
                adverse = adverse - self._offset
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._evaluate(adverse, "ws_candle_extreme"))
            except RuntimeError:
                pass

    # ──────────────────────────────────────────────────────────────────────
    # Exit dispatch (ASYNC, idempotent)
    # ──────────────────────────────────────────────────────────────────────
    async def _fire_exit(self, reason, price, src):
        if self._exit_fired:
            return
        self._exit_fired = True
        self._running    = False

        logger.info(f"[TRAIL] Exit fired: reason={reason} price={price:.2f} "
                    f"source={src} atr={self.atr:.2f} entry={self.entry_price:.2f} "
                    f"best={(self.best_price or 0):.2f}")

        is_sl = any(reason.startswith(r) for r in _SL_REASONS)

        if SL_FIRE_VIA_BRACKET and is_sl:
            logger.info("[TRAIL] SL_FIRE_VIA_BRACKET=true — leaving close to "
                        "Delta bracket; drift-check will record the exit.")
            return

        if self._order_mgr is not None:
            try:
                await self._order_mgr.close_position(is_long=self.is_long, reason=reason)
            except Exception as exc:
                logger.error(f"[TRAIL] close_position failed: {exc}", exc_info=True)

        if self._on_trail_exit is not None:
            try:
                await self._on_trail_exit(
                    exit_price = float(price),
                    reason     = reason,
                    source     = src,
                    position_already_closed = True,
                )
            except Exception as exc:
                logger.error(f"[TRAIL] on_trail_exit callback failed: {exc}", exc_info=True)
