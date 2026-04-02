"""
risk/calculator.py
SL/TP calculation, 5-stage trail ratchet, breakeven, max SL guard.

DELTA INDIA LOT CALCULATION (v11 FIX):
  On Delta Exchange India:
    0.1 BTC  = 100 lots
    1 lot    = 0.001 BTC

  P/L formula for USDT-margined BTC perpetual:
    raw_P/L  = (exit_price - entry_price) * qty_in_BTC
    qty_BTC  = lots * 0.001

  Example: 30 lots at entry 84000, exit 85000 (LONG):
    qty_BTC  = 30 * 0.001 = 0.03 BTC
    raw_P/L  = (85000 - 84000) * 0.03 = 30 USDT
"""

from dataclasses import dataclass
from typing import Tuple
from config import (
    TRAIL_STAGES,
    TREND_RR, RANGE_RR,
    TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS,
    BE_MULT,
    COMMISSION_PCT,
    ALERT_QTY,
)

# ── Delta India lot constants ──────────────────────────────────────────────────
# 0.1 BTC = 100 lots  →  1 lot = 0.001 BTC
DELTA_INDIA_BTC_PER_LOT = 0.001


def lots_to_btc(lots: int) -> float:
    return lots * DELTA_INDIA_BTC_PER_LOT


def btc_to_lots(btc: float) -> int:
    return int(btc / DELTA_INDIA_BTC_PER_LOT)


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

def calc_levels(close: float, atr: float, is_long: bool, is_trend: bool) -> RiskLevels:
    atr_mult  = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr        = TREND_RR       if is_trend else RANGE_RR
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    if stop_dist <= 0:
        stop_dist = atr * 0.5

    if is_long:
        sl = close - stop_dist
        tp = close + stop_dist * rr
    else:
        sl = close + stop_dist
        tp = close - stop_dist * rr

    return RiskLevels(entry_price=close, sl=sl, tp=tp, stop_dist=stop_dist,
                      atr=atr, is_long=is_long, is_trend=is_trend)


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


# ── P/L calculation (Delta India CORRECTED) ───────────────────────────────────

def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int   = ALERT_QTY,
    commission:  float = COMMISSION_PCT,
) -> float:
    """
    Correct P/L for Delta Exchange India.

    Delta India: 1 lot = 0.001 BTC  (0.1 BTC = 100 lots)
    raw_pl = price_move * qty_btc
    comm   = qty_btc * (entry + exit) * rate

    Example: 30 lots, entry=84000, exit=85000, LONG
      qty_btc = 0.03 BTC
      raw_pl  = 1000 * 0.03 = 30 USDT
      comm    = 0.03 * 169000 * 0.0005 = 2.535 USDT
      net     = 27.465 USDT
    """
    qty_btc = lots_to_btc(qty)
    if is_long:
        raw_pl = (exit_price - entry_price) * qty_btc
    else:
        raw_pl = (entry_price - exit_price) * qty_btc
    comm = qty_btc * (entry_price + exit_price) * commission
    return raw_pl - comm


def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int   = ALERT_QTY,
    commission:  float = COMMISSION_PCT,
) -> dict:
    """Full breakdown dict for Telegram messages and Google Sheets logging."""
    qty_btc  = lots_to_btc(qty)
    move     = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl   = move * qty_btc
    comm     = qty_btc * (entry_price + exit_price) * commission
    net_pl   = raw_pl - comm
    notional = entry_price * qty_btc
    pct      = (net_pl / notional * 100) if notional else 0.0

    return {
        "lots":            qty,
        "qty_btc":         round(qty_btc, 4),
        "price_move":      round(move, 2),
        "raw_pl_usdt":     round(raw_pl, 4),
        "commission_usdt": round(comm, 4),
        "net_pl_usdt":     round(net_pl, 4),
        "net_pl_pct":      round(pct, 4),
    }
