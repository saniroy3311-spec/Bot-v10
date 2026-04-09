"""
risk/calculator.py - Shiva Sniper v6.5
SL/TP calculation, 5-stage trail ratchet, breakeven, and P/L math.

FIXES vs previous version:
───────────────────────────────────────────────────────────────────────────────
FIX-PL-001 | P/L formula corrected to match Pine Script calcRealPL exactly.

  Root cause:
    Old formula: raw_pl_usd = qty * move / entry_price
    This is the INVERSE PERPETUAL BTC formula → result in BTC, NOT USD.
    Labelled "USDT" in logs but numerically off by a factor of ~entry_price.

    Example (entry=72056, exit=72504, qty=1 lot):
      Old  : 1 * 448 / 72056  = 0.00621  ← BTC (WRONG)
      New  : 1 * 0.001 * 448  = 0.448 USD ← matches Delta cashflow ✅

  Pine Script formula (calcRealPL):
    rawPL = (exitPx - entryPx) * qty_btc      ← linear, USD
    comm  = (entryPx + exitPx) * qty_btc * COMMISSION_PCT
    net   = rawPL - comm

  Where qty_btc = qty_lots * DELTA_INDIA_BTC_PER_LOT (= qty * 0.001)

FIX-PL-002 | Commission formula corrected.
  Old : 2 * qty * COMMISSION_PCT        (wrong units — qty in lots, no price)
  New : (entry + exit) * qty_btc * COMMISSION_PCT  (matches Pine exactly)
───────────────────────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass
from typing import Tuple
from config import (
    TRAIL_STAGES, TREND_RR, RANGE_RR,
    TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS, BE_MULT,
    COMMISSION_PCT, ALERT_QTY,
)

# Delta India BTC/USD perpetual: 1 lot = 0.001 BTC
DELTA_INDIA_BTC_PER_LOT = 0.001


def lots_to_btc(lots: int) -> float:
    return float(lots) * DELTA_INDIA_BTC_PER_LOT


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


def calc_levels(entry_price: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
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
    return calc_levels(fill_price, risk.atr, risk.is_long, risk.is_trend)


def calc_trail_stage(profit_dist: float, atr: float) -> int:
    """Calculate trail stage based on peak profit distance (PEAK profit, not current)."""
    stage = 0
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult = TRAIL_STAGES[i][0]
        if profit_dist >= atr * trigger_mult:
            stage = i + 1
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


# ─────────────────────────────────────────────────────────────────────────────
# FIX-PL-001 + FIX-PL-002: Pine Script calcRealPL — linear USD P/L
# ─────────────────────────────────────────────────────────────────────────────

def calc_real_pl(entry_price: float, exit_price: float,
                 is_long: bool, qty: int = ALERT_QTY) -> float:
    """
    P/L matching Pine Script's calcRealPL() exactly.

    Pine formula:
        rawPL = (exitPx - entryPx) * qty_btc
        comm  = (entryPx + exitPx) * qty_btc * COMMISSION_PCT
        net   = rawPL - comm

    Where qty_btc = qty_lots * 0.001 BTC/lot

    Example — entry=72056, exit=72504, qty=1 lot:
        qty_btc = 0.001 BTC
        raw_pl  = 448 * 0.001 = 0.448 USD
        comm    = 144560 * 0.001 * 0.0005 = 0.072 USD
        net_pl  = 0.376 USD
    """
    if entry_price <= 0 or exit_price <= 0:
        return 0.0

    qty_btc    = qty * DELTA_INDIA_BTC_PER_LOT
    move       = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl     = move * qty_btc
    # COMMISSION_PCT is one-way (0.0005 = 0.05%); we charge both entry + exit legs
    commission = (entry_price + exit_price) * qty_btc * COMMISSION_PCT
    return raw_pl - commission


def calc_pl_breakdown(entry_price: float, exit_price: float,
                      is_long: bool, qty: int = ALERT_QTY) -> dict:
    """Detailed P/L breakdown for journal and dashboard."""
    if entry_price <= 0 or exit_price <= 0:
        return {
            "lots": qty, "qty_btc": 0.0, "price_move": 0.0,
            "raw_pl_usdt": 0.0, "commission_usdt": 0.0,
            "net_pl_usdt": 0.0, "net_pl_pct": 0.0,
        }

    qty_btc         = qty * DELTA_INDIA_BTC_PER_LOT
    move            = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl          = move * qty_btc
    commission      = (entry_price + exit_price) * qty_btc * COMMISSION_PCT
    net_pl          = raw_pl - commission
    entry_notional  = entry_price * qty_btc
    net_pl_pct      = (net_pl / entry_notional * 100) if entry_notional else 0.0

    return {
        "lots":            qty,
        "qty_btc":         round(qty_btc, 6),
        "price_move":      round(move, 2),
        "raw_pl_usdt":     round(raw_pl, 4),
        "commission_usdt": round(commission, 4),
        "net_pl_usdt":     round(net_pl, 4),
        "net_pl_pct":      round(net_pl_pct, 4),
    }
