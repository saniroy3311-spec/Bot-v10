from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from typing import Callable, Optional

import ccxt.async_support as ccxt

from config import (
    DELTA_API_KEY, DELTA_API_SECRET, DELTA_TESTNET,
    SYMBOL, ALERT_QTY, CANDLE_TIMEFRAME,
    TRAIL_LOOP_SEC, TRAIL_SL_PRE_FIRE_BUFFER,
    BE_MULT,
)

from strategy_logic import (
    IndicatorSnapshot, Signal, SignalType,
    RiskLevels, TrailState,
    calc_levels, get_trail_params, upgrade_trail_stage,
    compute_trail_sl, should_trigger_be, max_sl_hit,
    calc_real_pl, signal_log_record,
)

logger = logging.getLogger("execution")


def _timeframe_to_ms(tf: str) -> int:
    tf = tf.strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1]) * 60 * 1000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    raise ValueError(f"Unknown timeframe: {tf}")


BAR_PERIOD_MS = _timeframe_to_ms(CANDLE_TIMEFRAME)
_INDIA_LIVE    = "https://api.india.delta.exchange"
_INDIA_TESTNET = "https://testnet-api.india.delta.exchange"

SIGNAL_LOG_PATH = os.environ.get("SIGNAL_LOG_PATH", "signals.jsonl")


def log_signal(snap: IndicatorSnapshot, sig: Signal, reason: str = "") -> None:
    rec = signal_log_record(snap, sig, reason)
    try:
        with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        logger.error(f"signal log write failed: {e}")


def build_exchange() -> ccxt.delta:
    base_url = _INDIA_TESTNET if DELTA_TESTNET else _INDIA_LIVE
    params = {
        "apiKey":          DELTA_API_KEY,
        "secret":          DELTA_API_SECRET,
        "enableRateLimit": True,
        "urls":            {"api": {"public": base_url, "private": base_url}},
    }
    return ccxt.delta(params)


async def _retry(coro_fn, retries: int = 3, delay: float = 1.0):
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            if attempt == retries:
                raise
            await asyncio.sleep(delay * (2 ** (attempt - 1)))


class OrderManager:
    def __init__(self):
        self.exchange = build_exchange()
        self.position: Optional[dict] = None

    async def initialize(self) -> None:
        await self.exchange.load_markets()
        if SYMBOL not in self.exchange.markets:
            raise ValueError(f"SYMBOL '{SYMBOL}' not found on Delta India.")

    async def place_entry(self, is_long: bool) -> dict:
        side = "buy" if is_long else "sell"
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
        ))
        fill_price = float(order.get("average") or order.get("price") or 0)
        self.position = {
            "entry_order_id": order["id"],
            "is_long":        is_long,
            "entry_price":    fill_price,
        }
        return order

    async def close_position(self, reason: str = "Exit") -> dict:
        if not self.position:
            return {}
        is_long = self.position["is_long"]
        side    = "sell" if is_long else "buy"
        order = await _retry(lambda: self.exchange.create_order(
            symbol = SYMBOL,
            type   = "market",
            side   = side,
            amount = ALERT_QTY,
            params = {"reduce_only": True},
        ))
        self.position = None
        return order

    async def fetch_position(self) -> Optional[dict]:
        positions = await _retry(lambda: self.exchange.fetch_positions([SYMBOL]))
        for pos in positions:
            if pos.get("symbol") == SYMBOL and pos.get("contracts", 0) != 0:
                return pos
        return None

    async def close_exchange(self) -> None:
        await self.exchange.close()


def candle_boundary(ts_ms: int) -> int:
    return (ts_ms // BAR_PERIOD_MS) * BAR_PERIOD_MS


class TrailMonitor:
    def __init__(self, order_manager: OrderManager, on_exit: Optional[Callable] = None):
        self.order_mgr = order_manager
        self.on_exit   = on_exit
        self.risk:  Optional[RiskLevels] = None
        self.state: Optional[TrailState] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._exchange = None
        self._exit_triggered = False
        self.entry_bar_boundary_ms: Optional[int] = None

    def _is_entry_bar(self, ts_ms: int) -> bool:
        if self.entry_bar_boundary_ms is None:
            return False
        return candle_boundary(ts_ms) == self.entry_bar_boundary_ms

    def start(self, risk: RiskLevels, state: TrailState,
              entry_bar_boundary_ms: int,
              on_exit: Optional[Callable] = None) -> None:
        self.risk  = risk
        self.state = state
        self.entry_bar_boundary_ms = entry_bar_boundary_ms
        if on_exit is not None:
            self.on_exit = on_exit
        self._exit_triggered = False
        if self.state.peak_price == 0.0:
            self.state.peak_price = risk.entry_price
        if self.state.current_sl == 0.0:
            self.state.current_sl = risk.sl
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"TrailMonitor start | entry={risk.entry_price:.2f} sl={risk.sl:.2f} "
            f"tp={risk.tp:.2f} atr={risk.atr:.2f} entry_bar_boundary={entry_bar_boundary_ms}"
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        try:
            await self._loop_rest()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"TrailMonitor crashed: {e}", exc_info=True)

    async def _loop_rest(self) -> None:
        self._exchange = build_exchange()
        try:
            while self._running:
                await asyncio.sleep(TRAIL_LOOP_SEC)
                try:
                    ticker = await self._exchange.fetch_ticker(SYMBOL)
                    price = float(
                        ticker.get("last")
                        or ticker.get("info", {}).get("mark_price")
                        or 0
                    )
                    if price > 0:
                        await self._on_tick(price)
                except Exception as e:
                    logger.error(f"tick error: {e}")
        finally:
            if self._exchange:
                await self._exchange.close()

    async def _on_tick(self, price: float) -> None:
        if not self._running or self.risk is None or self.state is None:
            return

        risk    = self.risk
        state   = self.state
        is_long = risk.is_long
        entry   = risk.entry_price
        atr     = risk.atr
        now_ms  = int(time.time() * 1000)

        if is_long:
            state.peak_price = max(state.peak_price, price)
            peak_profit_dist    = max(0.0, state.peak_price - entry)
            current_profit_dist = price - entry
        else:
            state.peak_price = min(state.peak_price, price)
            peak_profit_dist    = max(0.0, entry - state.peak_price)
            current_profit_dist = entry - price

        tp_hit = (is_long and price >= risk.tp) or (not is_long and price <= risk.tp)
        if tp_hit:
            await self._execute_exit(price, "Target Profit")
            return

        if not self._is_entry_bar(now_ms) and not state.max_sl_fired:
            if max_sl_hit(price, entry, atr, is_long):
                state.max_sl_fired = True
                await self._execute_exit(price, "Max SL Hit")
                return

        if not state.be_done and should_trigger_be(current_profit_dist, atr):
            state.be_done = True
            be_improves = (
                (is_long and entry > state.current_sl)
                or (not is_long and entry < state.current_sl)
            )
            if be_improves:
                state.current_sl = entry

        new_stage = upgrade_trail_stage(state.stage, peak_profit_dist, atr)
        if new_stage > state.stage:
            state.stage = new_stage

        trail_sl = compute_trail_sl(
            state.stage, state.peak_price, peak_profit_dist, is_long, atr
        )
        if trail_sl is not None:
            improves = (
                (is_long and trail_sl > state.current_sl)
                or (not is_long and trail_sl < state.current_sl)
            )
            if improves:
                state.current_sl = trail_sl

        if state.current_sl > 0:
            sl_hit = (
                (is_long and price <= state.current_sl + TRAIL_SL_PRE_FIRE_BUFFER)
                or (not is_long and price >= state.current_sl - TRAIL_SL_PRE_FIRE_BUFFER)
            )
            if sl_hit:
                trail_improved = (
                    (is_long and state.current_sl > risk.sl)
                    or (not is_long and state.current_sl < risk.sl)
                )
                be_at_entry = (
                    state.be_done and abs(state.current_sl - entry) <= max(1e-9, atr * 1e-6)
                )
                if trail_improved:
                    reason = f"Trail S{state.stage}"
                elif be_at_entry:
                    reason = "Breakeven SL"
                else:
                    reason = "Initial SL"
                await self._execute_exit(price, reason)
                return

    async def _execute_exit(self, price: float, reason: str) -> None:
        if self._exit_triggered:
            return
        self._exit_triggered = True
        self._running = False

        try:
            order = await self.order_mgr.close_position(reason=reason)
            fill = float(order.get("average") or order.get("price") or price)
        except Exception as e:
            logger.error(f"exit order failed: {e}")
            fill = price

        pl = calc_real_pl(self.risk.entry_price, fill, self.risk.is_long, ALERT_QTY)
        logger.info(
            f"Exit | reason={reason} entry={self.risk.entry_price:.2f} "
            f"exit={fill:.2f} pl={pl:.4f}"
        )
        if self.on_exit:
            try:
                await self.on_exit(fill, reason)
            except Exception as e:
                logger.error(f"on_exit cb failed: {e}")


class ExecutionEngine:
    def __init__(self):
        self.order_mgr  = OrderManager()
        self.trail_mon  = TrailMonitor(self.order_mgr)
        self.in_position = False
        self.risk: Optional[RiskLevels] = None
        self.trail_state: Optional[TrailState] = None
        self._last_signal_type: str = "None"
        self._entry_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self.order_mgr.initialize()

    async def shutdown(self) -> None:
        self.trail_mon.stop()
        await self.order_mgr.close_exchange()

    async def process_closed_bar(self, snap: IndicatorSnapshot, sig: Signal) -> None:
        log_signal(snap, sig, reason="bar_close_eval")

        if self.in_position:
            return

        if sig.signal_type == SignalType.NONE:
            return

        if self._entry_lock.locked():
            return

        async with self._entry_lock:
            if self.in_position:
                return

            risk_pre = calc_levels(snap.close, snap.atr, sig.is_long, sig.is_trend)

            try:
                order = await self.order_mgr.place_entry(sig.is_long)
            except Exception as e:
                logger.error(f"entry order failed: {e}")
                return

            fill = float(order.get("average") or order.get("price") or snap.close)
            risk = calc_levels(fill, snap.atr, sig.is_long, sig.is_trend)

            self.in_position = True
            self.risk = risk
            self._last_signal_type = sig.signal_type.value
            self.trail_state = TrailState(
                stage      = 0,
                current_sl = risk.sl,
                peak_price = fill,
            )

            entry_bar_boundary = candle_boundary(int(time.time() * 1000))

            self.trail_mon.start(
                risk, self.trail_state,
                entry_bar_boundary_ms = entry_bar_boundary,
                on_exit               = self._on_trail_exit,
            )

            log_signal(snap, sig, reason=f"ENTRY_FILL fill={fill:.2f}")
            logger.info(
                f"ENTRY | type={sig.signal_type.value} fill={fill:.2f} "
                f"sl={risk.sl:.2f} tp={risk.tp:.2f} atr={snap.atr:.2f}"
            )

    async def _on_trail_exit(self, exit_price: float, reason: str) -> None:
        if not self.in_position:
            return
        self.trail_mon.stop()
        pl = calc_real_pl(self.risk.entry_price, exit_price, self.risk.is_long, ALERT_QTY) if self.risk else 0.0
        logger.info(
            f"EXIT | reason={reason} entry={self.risk.entry_price:.2f} "
            f"exit={exit_price:.2f} pl={pl:+.4f}"
        )
        try:
            with open(SIGNAL_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp":   int(time.time() * 1000),
                    "signal_type": "EXIT",
                    "reason":      reason,
                    "entry_price": self.risk.entry_price if self.risk else None,
                    "exit_price":  exit_price,
                    "pl":          pl,
                }) + "\n")
        except Exception:
            pass
        self.in_position = False
        self.risk = None
        self.trail_state = None
