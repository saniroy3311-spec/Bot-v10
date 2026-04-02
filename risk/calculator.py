"""
risk/calculator.py - Shiva Sniper v6.5
SL/TP calculation, 5-stage trail ratchet, breakeven, and P/L math.
"""

from dataclasses import dataclass
from typing import Tuple
from config import (
    TRAIL_STAGES, TREND_RR, RANGE_RR,
    TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS, BE_MULT,
    COMMISSION_PCT, ALERT_QTY,
)

# ── DELTA INDIA CONSTANTS ───────────────────────────────────────────────────
# 1 lot = 0.001 BTC on Delta India (Inverse Perpetual)
DELTA_INDIA_BTC_PER_LOT = 0.001

def lots_to_btc(lots: int) -> float:
    """Convert number of lots to actual BTC quantity."""
    return float(lots) * DELTA_INDIA_BTC_PER_LOT

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class RiskLevels:
    entry_price: float
    sl:          float
    tp:          float
    stop_dist:   float
    atr:         float
    is_long:     bool
    is_trend:    bool

@dataclass
class TrailState:
    stage:        int   = 0
    current_sl:   float = 0.0
    peak_price:   float = 0.0
    be_done:      bool  = False
    max_sl_fired: bool  = False

# ── Entry-level calculations ───────────────────────────────────────────────────

def calc_levels(entry_price: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
    """
    Calculate SL/TP anchored to entry_price.
    Matches Pine Script: anchors SL/TP to the actual fill price.
    """
    atr_mult  = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr        = TREND_RR       if is_trend else RANGE_RR
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    
    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + stop_dist * rr
    else:
        sl = entry_price + stop_dist
        tp = entry_price - stop_dist * rr

    return RiskLevels(
        entry_price=entry_price,
        sl=sl,
        tp=tp,
        stop_dist=stop_dist,
        atr=atr,
        is_long=is_long,
        is_trend=is_trend
    )

def recalc_levels_from_fill(risk: RiskLevels, fill_price: float) -> RiskLevels:
    """FIX-RISK-1: Recalculate SL/TP anchored to actual fill price."""
    return calc_levels(fill_price, risk.atr, risk.is_long, risk.is_trend)

# ── Trail engine ───────────────────────────────────────────────────────────────

def calc_trail_stage(profit_dist: float, atr: float) -> int:
    stage = 0
    for i, (trigger_mult, _, _) in enumerate(TRAIL_STAGES):
        if profit_dist >= atr * trigger_mult:
            stage = i + 1
        else:
            break
    return stage

def get_trail_params(stage: int, atr: float) -> Tuple[float, float]:
    if stage < 1 or stage > len(TRAIL_STAGES):
        raise ValueError(f"trail stage must be 1-{len(TRAIL_STAGES)}, got {stage}")
    _, points_mult, offset_mult = TRAIL_STAGES[stage - 1]
    return atr * points_mult, atr * offset_mult

def should_trigger_be(profit_dist: float, atr: float) -> bool:
    return profit_dist >= atr * BE_MULT

def max_sl_hit(current_price: float, entry_price: float, atr: float, is_long: bool) -> bool:
    loss_cap = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    if is_long:
        return current_price <= entry_price - loss_cap
    else:
        return current_price >= entry_price + loss_cap

# ── P/L calculation ────────────────────────────────────────────────────────────

def calc_real_pl(entry_price: float, exit_price: float, is_long: bool, qty: int = ALERT_QTY) -> float:
    """
    FIX-RISK-2: Correct P/L for Delta India BTC/USD:USD inverse perpetual.
    Formula: qty * (exit - entry) / entry
    """
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    
    move = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl_usd = qty * move / entry_price
    
    # Commission: 0.05% per side (Total 0.1% round trip)
    commission_usd = 2 * qty * COMMISSION_PCT
    
    return raw_pl_usd - commission_usd

def calc_pl_breakdown(entry_price: float, exit_price: float, is_long: bool, qty: int = ALERT_QTY) -> dict:
    """Full breakdown dict for Telegram messages and logging."""
    if entry_price <= 0 or exit_price <= 0:
        return {
            "lots": qty, "qty_btc": 0, "price_move": 0, "raw_pl_usdt": 0,
            "commission_usdt": 0, "net_pl_usdt": 0, "net_pl_pct": 0,
        }

    move = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl = qty * move / entry_price
    comm   = 2 * qty * COMMISSION_PCT
    net_pl = raw_pl - comm
    
    return {
        "lots":            qty,
        "qty_btc":         lots_to_btc(qty),
        "price_move":      round(move, 2),
        "raw_pl_usdt":     round(raw_pl, 4),
        "commission_usdt": round(comm, 4),
        "net_pl_usdt":     round(net_pl, 4),
        "net_pl_pct":      round((net_pl / qty * 100), 4) if qty else 0.0
    }
