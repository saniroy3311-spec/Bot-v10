"""
risk/calculator.py — Shiva Sniper v6.5 (FIX-CALC-v4)

Functions
─────────
  lots_to_btc              Convert lots → BTC qty
  calc_levels              SL/TP from bar-close price (pre-fill estimate)
  recalc_levels_from_fill  Re-anchor SL/TP to ACTUAL fill price  ← FIX-MAIN-1
  calc_pl_breakdown        Full P/L dict (all 8 keys Telegram needs)
  calc_real_pl             Net P/L scalar
  max_sl_hit               Max-SL guard boolean
  max_sl_exit_price        Price at which Max SL triggers
  get_trail_params         Trail pts/offset for a given stage
  calc_trail_stage         Highest stage activated by profit distance
  should_trigger_be        Breakeven trigger boolean
"""

from dataclasses import dataclass
from config import (
    TREND_RR, RANGE_RR,
    TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS,
    TRAIL_STAGES, BE_MULT,
    COMMISSION_PCT,
)

# ── Constants ──────────────────────────────────────────────────────────────────
DELTA_INDIA_BTC_PER_LOT: float = 0.001   # 1 lot = 0.001 BTC on Delta India


# ── Basic helpers ──────────────────────────────────────────────────────────────
def lots_to_btc(lots: int) -> float:
    """Convert Delta India lots to BTC quantity."""
    return lots * DELTA_INDIA_BTC_PER_LOT


# ── Data classes ───────────────────────────────────────────────────────────────
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


# ── Level calculators ──────────────────────────────────────────────────────────
def calc_levels(
    entry_price: float,
    atr:         float,
    is_long:     bool,
    is_trend:    bool,
) -> RiskLevels:
    """
    Compute SL / TP from the bar-close price (used at signal time,
    before we know the actual fill price).
    """
    rr        = TREND_RR       if is_trend else RANGE_RR
    atr_mult  = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)

    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + stop_dist * rr
    else:
        sl = entry_price + stop_dist
        tp = entry_price - stop_dist * rr

    return RiskLevels(entry_price, sl, tp, stop_dist, atr, is_long, is_trend)


def recalc_levels_from_fill(risk: RiskLevels, fill_price: float) -> RiskLevels:
    """
    FIX-MAIN-1: Re-anchor SL and TP to the ACTUAL fill price.

    Pine Script uses strategy.position_avg_price (the fill) to set
    longSL / longTP, not the bar close.  Without this fix the bot's
    SL/TP are slightly offset from TradingView whenever there is
    slippage.

    Keeps the same stop_dist, ATR, direction, and regime as the
    original calc_levels() call — only entry_price, sl, tp change.
    """
    stop_dist = risk.stop_dist
    rr        = TREND_RR if risk.is_trend else RANGE_RR

    if risk.is_long:
        sl = fill_price - stop_dist
        tp = fill_price + stop_dist * rr
    else:
        sl = fill_price + stop_dist
        tp = fill_price - stop_dist * rr

    return RiskLevels(
        entry_price = fill_price,
        sl          = sl,
        tp          = tp,
        stop_dist   = stop_dist,
        atr         = risk.atr,
        is_long     = risk.is_long,
        is_trend    = risk.is_trend,
    )


# ── P/L calculators ────────────────────────────────────────────────────────────
def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty_lots:    int,
) -> dict:
    """
    Full P/L breakdown.
    Returns all 8 keys that telegram.notify_exit() expects:
      raw_pl_usdt, comm_usdt, commission_usdt, net_pl_usdt,
      qty_btc, price_move, lots, net_pl_pct
    """
    qty_btc    = lots_to_btc(qty_lots)
    price_move = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    raw_pl     = price_move * qty_btc
    comm       = (entry_price + exit_price) * qty_btc * (COMMISSION_PCT * 2)
    net_pl     = raw_pl - comm
    notional   = entry_price * qty_btc
    net_pl_pct = (net_pl / notional * 100) if notional > 0 else 0.0

    return {
        "raw_pl_usdt"    : raw_pl,
        "comm_usdt"      : comm,
        "commission_usdt": comm,
        "net_pl_usdt"    : net_pl,
        "qty_btc"        : qty_btc,
        "price_move"     : price_move,
        "lots"           : qty_lots,
        "net_pl_pct"     : net_pl_pct,
    }


def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty_lots:    int,
) -> float:
    """Net P/L in USDT after round-trip commission."""
    qty_btc = lots_to_btc(qty_lots)
    raw_pl  = (exit_price - entry_price) * qty_btc if is_long \
              else (entry_price - exit_price) * qty_btc
    comm    = (entry_price + exit_price) * qty_btc * (COMMISSION_PCT * 2)
    return raw_pl - comm


# ── Max SL helpers ─────────────────────────────────────────────────────────────
def max_sl_hit(
    current_price: float,
    entry_price:   float,
    atr:           float,
    is_long:       bool,
) -> bool:
    """True when price has moved MAX_SL_MULT x ATR against the trade."""
    threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    if is_long:
        return current_price <= entry_price - threshold
    return current_price >= entry_price + threshold


def max_sl_exit_price(
    entry_price: float,
    atr:         float,
    is_long:     bool,
) -> float:
    """Exact price level at which Max SL triggers."""
    threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    return (entry_price - threshold) if is_long else (entry_price + threshold)


# ── Trail helpers (also used by Phase 2 QC scripts) ───────────────────────────
def get_trail_params(stage: int, atr: float) -> tuple:
    """
    Return (trail_points, trail_offset) for a given trail stage.
      trail_points : profit distance required to ACTIVATE this stage
      trail_offset : distance from peak where the SL sits
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def calc_trail_stage(profit_dist: float, atr: float) -> int:
    """Return the highest trail stage activated by profit_dist."""
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if profit_dist >= atr * trigger_mult:
            return i + 1
    return 0


def should_trigger_be(profit_dist: float, atr: float) -> bool:
    """True when profit has moved enough to set breakeven."""
    return profit_dist > atr * BE_MULT
