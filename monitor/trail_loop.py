"""
monitor/trail_loop.py — Shiva Sniper v10 trailing-stop engine
================================================================

REWRITE (2026-06-02): the previous file in the repo exposed an OLD interface
(`start(entry_price, risk, is_long)`, `bot.handle_trail_exit`, sync
`on_price_tick`, no `on_bar_close`, `_running` never set True). main.py and
both feeds were written for a NEWER interface, so every entry crashed with:

    TrailMonitor.start() got an unexpected keyword argument 'risk_levels'

…the trail never started, the position sat behind the static emergency
bracket, and the bracket fired silently (recovered by the drift-check as a
loss). This file restores the interface the rest of the codebase calls.

PUBLIC INTERFACE (consumed by main.py / feeds — DO NOT rename):
    TrailMonitor(order_mgr=, telegram=, journal=)
    .start(risk_levels, trail_state, entry_bar_time_ms, on_trail_exit,
           signal_bar_high=, signal_bar_low=, signal_bar_open=,
           signal_bar_close=, entry_wall_ms=)
    .stop()
    .on_bar_close(bar_close, bar_high, bar_low, bar_open, current_atr)   # sync
    async .on_price_tick(price, source="binance"|"delta")
    async .push_delta_tick(price)
    .push_ws_candle(high, low, source=, close=None)                       # sync
    ._running   (bool)  — True while a trade is being trailed
    ._exit_fired(bool)  — True once an exit has been dispatched (idempotent)

EXIT OWNERSHIP:
    main.py._on_trail_exit() only does bookkeeping (journal / Telegram /
    WhatsApp / state reset) — it sends NO orders. So this engine must close
    the Delta position itself (order_mgr.close_position) BEFORE invoking the
    callback with position_already_closed=True.

PINE-PARITY NOTES (preserved from Master Memory bug history):
    • Trail math uses the ENTRY-bar ATR (risk_levels.atr), never live ATR —
      live ATR drift after entry destroys parity (FIX-RECOVERY).
    • best_price is seeded from the ENTRY price, never from the signal bar
      OHLC — seeding from the signal bar fired the trail on tick 1 (Bug 10).
    • Stage upgrades + breakeven only advance at bar close (Pine
      calc_on_every_tick=false). Trail SL only FIRES on live ticks (Bug 11).
    • push_ws_candle advances best_price from the FAVOURABLE extreme only;
      it never fires the stop unless TRAIL_FIRE_SL_ON_CANDLE_EXTREME=true
      (the stale cumulative-high bug, FIX-STALE-CANDLE-HIGH).
    • Trail offset has an ATR floor so it can never trail tighter than tick
      noise (Bug 16 / FIX-TICK-NOISE-WHIPSAW).
"""

import time
import asyncio
import logging

from config import (
    PINE_MINTICK,
    TRAIL_OFFSET_FLOOR_MULT,
    TRAIL_ARM_FLOOR_MULT,
    TIME_EXIT_MINUTES,
    TRAIL_FIRE_SL_ON_CANDLE_EXTREME,
    TRAIL_STAGES,
    BE_MULT,
    MAX_SL_MULT,
    MAX_SL_POINTS,
    SL_FIRE_VIA_BRACKET,
    TRAIL_SL_PRE_FIRE_BUFFER,
)

logger = logging.getLogger("trail_loop")

# Reasons that represent a stop-loss-type cross (as opposed to TP). Used to
# decide whether Delta's bracket owns the exit when SL_FIRE_VIA_BRACKET=true.
_SL_REASONS = ("Initial SL", "Trail SL", "Max SL")


class TrailMonitor:
    # ──────────────────────────────────────────────────────────────────────
    # Construction
    # ──────────────────────────────────────────────────────────────────────
    def __init__(self, order_mgr=None, telegram=None, journal=None, **kwargs):
        self._order_mgr = order_mgr
        self._telegram  = telegram
        self._journal   = journal

        # Lifecycle flags read by main.py and both feeds.
        self._running    = False
        self._exit_fired = False

        # Per-trade state (assigned in start()).
        self._risk          = None          # risk.calculator.RiskLevels
        self._state         = None          # risk.calculator.TrailState (mutated in place)
        self._on_trail_exit = None          # async callback supplied by main.py
        self.is_long        = False
        self.entry_price    = 0.0
        self.atr            = 0.0           # ENTRY-bar ATR — frozen for the trade
        self.tp             = 0.0
        self._entry_time_ms = 0

        # Live trail tracking.
        self.best_price   = None            # favourable extreme since entry
        self.trail_armed  = False
        self.trail_sl     = None
        self.be_done      = False
        self.max_sl_fired = False

        # Binance→Delta offset (FIX-DUAL-SOURCE-B). Locked once after entry so
        # mid-trade recalibration can't slide the trail (Bug 10 / Bug 16).
        self._offset          = 0.0
        self._offset_locked   = False
        self._last_delta_seen = None

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
        """Arm the trail for a freshly-filled (or recovered) position."""
        self._risk          = risk_levels
        self._state         = trail_state
        self._on_trail_exit = on_trail_exit

        self.is_long     = bool(risk_levels.is_long)
        self.entry_price = float(risk_levels.entry_price)
        self.atr         = float(risk_levels.atr) if getattr(risk_levels, "atr", 0) else 250.0
        self.tp          = float(getattr(risk_levels, "tp", 0.0) or 0.0)

        # Seed from the TrailState (recovery keeps original SL / stage), else
        # from the entry levels. best_price = ENTRY price (never signal bar).
        if trail_state is not None:
            self.trail_sl     = float(trail_state.current_sl) if trail_state.current_sl else float(risk_levels.sl)
            self.best_price   = float(trail_state.peak_price) if trail_state.peak_price else self.entry_price
            self.be_done      = bool(getattr(trail_state, "be_done", False))
            self.max_sl_fired = bool(getattr(trail_state, "max_sl_fired", False))
            self.trail_armed  = int(getattr(trail_state, "stage", 0)) > 0
        else:
            self.trail_sl     = float(risk_levels.sl)
            self.best_price   = self.entry_price
            self.be_done      = False
            self.max_sl_fired = False
            self.trail_armed  = False

        # Time-exit clock: prefer the original wall time on recovery so the
        # cap counts from the real entry, not the restart.
        self._entry_time_ms = int(entry_wall_ms or entry_bar_time_ms or time.time() * 1000)

        # Reset offset calibration for the new trade.
        self._offset          = 0.0
        self._offset_locked   = False
        self._last_delta_seen = None

        self._exit_fired = False
        self._running    = True

        stage = int(getattr(trail_state, "stage", 0)) if trail_state else 0
        logger.info(
            f"[TRAIL] Started | entry={self.entry_price:.2f} sl={self.trail_sl:.2f} "
            f"tp={self.tp:.2f} entry_atr={self.atr:.2f} is_long={self.is_long} "
            f"stage={stage} armed={self.trail_armed}"
        )

        # Informational only — NOT fed into the trail math (Bug 6 / Bug 10/11).
        if signal_bar_high is not None and signal_bar_low is not None:
            logger.info(
                f"[TRAIL] Signal bar OHLC (informational only) | "
                f"high={signal_bar_high:.2f} low={signal_bar_low:.2f} "
                f"close={(signal_bar_close or 0.0):.2f}"
            )

    def stop(self):
        """Halt trailing. Safe to call multiple times."""
        self._running = False
        logger.info("TrailMonitor stopped.")

    # ──────────────────────────────────────────────────────────────────────
    # Stage / offset helpers (use ENTRY ATR — frozen for the trade)
    # ──────────────────────────────────────────────────────────────────────
    def _favorable_profit(self, ref_price):
        """Distance from entry in the favourable direction (points, >=0)."""
        if self.is_long:
            return ref_price - self.entry_price
        return self.entry_price - ref_price

    def _stage_offset(self):
        """ATR-scaled trail offset for the current stage, with the noise floor."""
        stage = int(getattr(self._state, "stage", 0)) if self._state else 0
        if stage < 1:
            off_mult = TRAIL_STAGES[0][2]
        else:
            off_mult = TRAIL_STAGES[min(stage, len(TRAIL_STAGES)) - 1][2]
        raw   = self.atr * off_mult * PINE_MINTICK
        floor = self.atr * TRAIL_OFFSET_FLOOR_MULT
        return max(raw, floor)

    def _recompute_trail_sl(self, src):
        """Tighten the trail SL toward best_price. Only ever moves in favour."""
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
                f"[TRAIL] Trail SL: {old:.2f} → {self.trail_sl:.2f} "
                f"(stage {stage} best={self.best_price:.2f} off={offset:.2f} src={src})"
            )
        self._sync_state()

    def _sync_state(self):
        """Mirror live values into the shared TrailState main.py reads."""
        if self._state is None:
            return
        if self.trail_sl is not None:
            self._state.current_sl = float(self.trail_sl)
        if self.best_price is not None:
            self._state.peak_price = float(self.best_price)
        self._state.be_done      = self.be_done
        self._state.max_sl_fired = self.max_sl_fired

    def _update_best(self, price):
        """Advance the favourable extreme only. Returns True if it moved."""
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
    # Bar close — stage upgrades + breakeven (SYNC, called un-awaited)
    # ──────────────────────────────────────────────────────────────────────
    def on_bar_close(self, bar_close, bar_high, bar_low, bar_open=None, current_atr=None):
        """
        Pine evaluates stage upgrades and breakeven at bar close only. This is
        sync and never fires an exit — actual SL/TP firing happens on the next
        live tick (on_price_tick), which is correct Pine parity for bar-close
        entries. current_atr is logged for diagnostics but NOT used for the
        trail math (entry ATR is frozen for parity).
        """
        if not self._running or self._state is None:
            return

        # Advance peak from the favourable bar extreme.
        self._update_best(bar_high if self.is_long else bar_low)

        profit = self._favorable_profit(self.best_price)

        # ── Stage upgrades (raw ATR multiples — no mintick) ───────────────────
        stage = int(getattr(self._state, "stage", 0))
        upgraded = False
        while stage < len(TRAIL_STAGES) and profit >= self.atr * TRAIL_STAGES[stage][0]:
            stage += 1
            upgraded = True
        if upgraded:
            self._state.stage = stage
            self.trail_armed  = True
            logger.info(
                f"[TRAIL] Stage → {stage} | profit={profit:.2f} "
                f"(trigger={self.atr * TRAIL_STAGES[stage - 1][0]:.2f} atr={self.atr:.2f})"
            )

        # ── Breakeven (once per trade) ────────────────────────────────────────
        if not self.be_done and profit >= self.atr * BE_MULT:
            self.be_done = True
            if self.is_long:
                self.trail_sl = max(self.trail_sl or self.entry_price, self.entry_price)
            else:
                self.trail_sl = min(self.trail_sl or self.entry_price, self.entry_price)
            logger.info(f"[TRAIL] Breakeven armed | sl→{self.trail_sl:.2f} profit={profit:.2f}")

        # Recompute trail SL from the (possibly new) stage offset.
        self._recompute_trail_sl(src="bar_close")
        self._sync_state()

        logger.info(
            f"[TRAIL] Bar close | best={self.best_price:.2f} sl={self.trail_sl:.2f} "
            f"stage={stage} armed={self.trail_armed} live_atr={(current_atr or 0):.2f}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Live ticks — exit firing (ASYNC)
    # ──────────────────────────────────────────────────────────────────────
    async def on_price_tick(self, price, source="binance"):
        """Primary intrabar exit path. Binance prices are translated into Delta
        space via the locked offset; Delta prices are used as-is."""
        if not self._running or self._exit_fired:
            return

        if source == "delta":
            self._last_delta_seen = float(price)
            px = float(price)
        else:  # binance
            # Lock the Binance→Delta offset once, using the latest Delta tick.
            if not self._offset_locked and self._last_delta_seen is not None:
                self._offset        = float(price) - float(self._last_delta_seen)
                self._offset_locked = True
                logger.info(f"[TRAIL] Offset locked: {self._offset:+.2f} "
                            f"(binance={price:.2f} delta={self._last_delta_seen:.2f})")
            px = float(price) - self._offset

        await self._evaluate(px, source)

    async def push_delta_tick(self, price):
        """Delta-native price (no offset). Scheduled via create_task by ws_feed."""
        self._last_delta_seen = float(price)
        if not self._running or self._exit_fired:
            return
        await self._evaluate(float(price), "delta")

    async def _evaluate(self, price, src):
        """Update peak, tighten trail, then check time / SL / TP / Max-SL."""
        # ── Time exit ─────────────────────────────────────────────────────────
        if TIME_EXIT_MINUTES > 0:
            elapsed_ms = int(time.time() * 1000) - self._entry_time_ms
            if elapsed_ms >= TIME_EXIT_MINUTES * 60_000:
                await self._fire_exit(f"Time exit ({TIME_EXIT_MINUTES}m)", price, src)
                return

        # ── Arm on first favourable push past stage-1 activation ──────────────
        if not self.trail_armed:
            arm_pts = max(self.atr * TRAIL_STAGES[0][1] * PINE_MINTICK, self.atr * TRAIL_ARM_FLOOR_MULT)
            if self._favorable_profit(price) >= arm_pts:
                self._update_best(price)
                self.trail_armed = True
                if self._state is not None and self._state.stage < 1:
                    self._state.stage = 1
                self._recompute_trail_sl(src=src)
                logger.info(
                    f"[TRAIL] Trail ARMED | price={price:.2f} arm_pts={arm_pts:.2f} "
                    f"trail_sl={self.trail_sl:.2f}"
                )
        else:
            if self._update_best(price):
                self._recompute_trail_sl(src=src)

        # ── Max SL circuit breaker (hard cap from entry) ──────────────────────
        if not self.max_sl_fired:
            adverse = (self.entry_price - price) if self.is_long else (price - self.entry_price)
            max_dist = min(self.atr * MAX_SL_MULT, MAX_SL_POINTS)
            if adverse >= max_dist:
                self.max_sl_fired = True
                self._sync_state()
                await self._fire_exit("Max SL", price, src)
                return

        # ── Stop / take-profit crosses ────────────────────────────────────────
        buf = TRAIL_SL_PRE_FIRE_BUFFER
        if self.is_long:
            if self.trail_sl is not None and price <= self.trail_sl + buf:
                await self._fire_exit("Trail SL" if self.trail_armed else "Initial SL", self.trail_sl, src)
            elif self.tp and price >= self.tp:
                await self._fire_exit("Take Profit", self.tp, src)
        else:
            if self.trail_sl is not None and price >= self.trail_sl - buf:
                await self._fire_exit("Trail SL" if self.trail_armed else "Initial SL", self.trail_sl, src)
            elif self.tp and price <= self.tp:
                await self._fire_exit("Take Profit", self.tp, src)

    # ──────────────────────────────────────────────────────────────────────
    # WS candle — favourable peak only (SYNC)
    # ──────────────────────────────────────────────────────────────────────
    def push_ws_candle(self, high, low, source="binance", close=None, **kwargs):
        """
        Advance best_price from the FAVOURABLE extreme only (low for a short,
        high for a long) and tighten the trail. Never fires the stop on the
        adverse extreme unless TRAIL_FIRE_SL_ON_CANDLE_EXTREME=true — that was
        the stale cumulative-high bug. This is sync; if the (rare) candle-fire
        path is enabled, the exit is scheduled on the event loop.
        """
        if not self._running or self._exit_fired:
            return

        favorable = high if self.is_long else low
        # Candle highs/lows from Binance are in Binance space — translate.
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
                pass  # no running loop — caller will tick again shortly

    # ──────────────────────────────────────────────────────────────────────
    # Exit dispatch (ASYNC, idempotent)
    # ──────────────────────────────────────────────────────────────────────
    async def _fire_exit(self, reason, price, src):
        if self._exit_fired:
            return
        self._exit_fired = True
        self._running    = False

        logger.info(f"[TRAIL] Exit fired: reason={reason} price={price:.2f} "
                    f"source={src} atr={self.atr:.2f}")

        is_sl = any(reason.startswith(r) for r in _SL_REASONS)

        # When the bracket SL owns SL crosses, do NOT send a market close — the
        # Delta bracket fills at matching-engine speed and main.py's bar-close
        # drift-check records the exit. Avoids double-closing the position.
        if SL_FIRE_VIA_BRACKET and is_sl:
            logger.info("[TRAIL] SL_FIRE_VIA_BRACKET=true — leaving close to "
                        "Delta bracket; drift-check will record the exit.")
            return

        # Otherwise the engine closes the Delta position itself, then hands off
        # to main.py for journal / Telegram / state reset.
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
