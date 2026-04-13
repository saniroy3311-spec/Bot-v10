"""
risk/calculator.py — Shiva Sniper v6.5 (FIX-CALC-v2)
"""

from dataclasses import dataclass
from config import (
    TREND_RR, RANGE_RR, TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS, TRAIL_STAGES, BE_MULT,
    COMMISSION_PCT,
)

DELTA_INDIA_BTC_PER_LOT = 0.001

def lots_to_btc(lots: int) -> float:
    return lots * DELTA_INDIA_BTC_PER_LOT

def calc_pl_breakdown(entry_price: float, exit_price: float, is_long: bool, qty_lots: int) -> dict:
    qty_btc = lots_to_btc(qty_lots)
    price_move = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl = price_move * qty_btc
    comm = (entry_price + exit_price) * qty_btc * (COMMISSION_PCT * 2)
    net_pl = raw_pl - comm
    notional = entry_price * qty_btc
    net_pl_pct = (net_pl / notional) * 100 if notional > 0 else 0
    return {
        "raw_pl_usdt": raw_pl,
        "comm_usdt": comm,
        "commission_usdt": comm,
        "net_pl_usdt": net_pl,
        "qty_btc": qty_btc,
        "price_move": price_move,
        "lots": qty_lots,
        "net_pl_pct": net_pl_pct,
    }

@dataclass
class RiskLevels:
    entry_price: float
    sl: float
    tp: float
    stop_dist: float
    atr: float
    is_long: bool
    is_trend: bool

@dataclass
class TrailState:
    stage: int = 0
    current_sl: float = 0.0
    peak_price: float = 0.0
    be_done: bool = False
    max_sl_fired: bool = False

def calc_levels(entry_price: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
    rr = TREND_RR if is_trend else RANGE_RR
    atr_mult = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    if is_long:
        sl, tp = entry_price - stop_dist, entry_price + stop_dist * rr
    else:
        sl, tp = entry_price + stop_dist, entry_price - stop_dist * rr
    return RiskLevels(entry_price, sl, tp, stop_dist, atr, is_long, is_trend)

def max_sl_hit(current_price: float, entry_price: float, atr: float, is_long: bool) -> bool:
    threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    return (current_price <= entry_price - threshold) if is_long else (current_price >= entry_price + threshold)

def calc_real_pl(entry_price: float, exit_price: float, is_long: bool, qty_lots: int) -> float:
    qty_btc = lots_to_btc(qty_lots)
    raw_pl = (exit_price - entry_price) * qty_btc if is_long else (entry_price - exit_price) * qty_btc
    comm = (entry_price + exit_price) * qty_btc * (COMMISSION_PCT * 2)
    return raw_pl - comm
