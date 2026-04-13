"""
risk/calculator.py — Shiva Sniper v6.5-30M
Exact replica of Pine Script risk calculations.

═══════════════════════════════════════════════════════════════════
PINE SCRIPT RISK FORMULAS (verbatim):
═══════════════════════════════════════════════════════════════════

  isTrend  = strategy.opentrades.entry_id(0) in ("Trend Long","Trend Short")
  rr       = isTrend ? trendRR     : rangeRR
  atrMult  = isTrend ? trendATRmul : rangeATRmul

  stopDist = math.min(atr * atrMult, maxSLPoints)
  longSL   = entryPrice - stopDist
  longTP   = entryPrice + stopDist * rr
  shortSL  = entryPrice + stopDist
  shortTP  = entryPrice - stopDist * rr

  // Breakeven
  beTrigger = atr * beMult
  if close - entryPrice > beTrigger → move SL to entryPrice

  // Max SL
  maxSLdynPts = atr * maxSLMult
  if close <= entryPrice - min(maxSLdynPts, maxSLPoints) → close_all

  // Commission-adjusted P/L
  rawPL = isLong ? (exitPx - entryPx) * qty : (entryPx - exitPx) * qty
  comm  = (entryPx + exitPx) * qty * 0.001
  realPL = rawPL - comm

═══════════════════════════════════════════════════════════════════
5-STAGE TRAIL PARAMS (from Pine inputs, 30M-OPT):
═══════════════════════════════════════════════════════════════════

  Stage 1: trigger=1.0 ATR, pts=0.70 ATR, off=0.55 ATR
  Stage 2: trigger=2.0 ATR, pts=0.55 ATR, off=0.45 ATR
  Stage 3: trigger=3.0 ATR, pts=0.45 ATR, off=0.35 ATR
  Stage 4: trigger=5.0 ATR, pts=0.30 ATR, off=0.25 ATR
  Stage 5: trigger=8.0 ATR, pts=0.20 ATR, off=0.15 ATR

  Pine strategy.exit():
    trail_points = activePts  (how far in profit to activate trail)
    trail_offset = activeOff  (distance behind PEAK where SL sits)
    Trail SL = peak_price - activeOff  [long]
    Trail SL = peak_price + activeOff  [short]

═══════════════════════════════════════════════════════════════════
BUG HISTORY:
═══════════════════════════════════════════════════════════════════
  BUG-RISK-001 | calc_trail_stage returned stage based on profit_dist
               but get_trail_params returned wrong tuple order, causing
               paper_engine to use `pts` as SL distance (should be `off`).
               FIX: get_trail_params returns (pts, off); paper_engine now
               uses `off` (index 1) not `pts` (index 0) for trail SL.

  BUG-RISK-002 | max_sl_hit used hardcoded 1.5× multiplier and 500pt cap.
               Pine: maxSLmult=2.0, maxSLPoints=1500.
               FIX: use config values MAX_SL_MULT=2.0, MAX_SL_POINTS=1500.
"""

from dataclasses import dataclass, field
from typing import Optional
from config import (
    TREND_RR, RANGE_RR, TREND_ATR_MULT, RANGE_ATR_MULT,
    MAX_SL_MULT, MAX_SL_POINTS, TRAIL_STAGES, BE_MULT,
    COMMISSION_PCT, ALERT_QTY,
)

# ─────────────────────────────────────────────────────────────────────────────
# Delta India lot size constant
# 1 lot = 0.001 BTC on Delta Exchange India
# ─────────────────────────────────────────────────────────────────────────────
DELTA_INDIA_BTC_PER_LOT = 0.001


def lots_to_btc(lots: int) -> float:
    """Convert Delta India lots to BTC quantity."""
    return lots * DELTA_INDIA_BTC_PER_LOT


def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty_lots:    int,
) -> dict:
    """
    Full P/L breakdown matching Pine Script commission formula.
    Returns dict with raw_pl_usdt, comm_usdt, net_pl_usdt, qty_btc.

    Pine:
      rawPL = isLong ? (exitPx - entryPx) * qty : (entryPx - exitPx) * qty
      comm  = (entryPx + exitPx) * qty * 0.001  (0.05% × 2 sides)
      realPL = rawPL - comm

    Here qty is in BTC (lots × 0.001).
    """
    qty_btc     = lots_to_btc(qty_lots)
    price_move  = (exit_price - entry_price) if is_long \
                  else (entry_price - exit_price)
    raw_pl      = price_move * qty_btc
    comm        = (entry_price + exit_price) * qty_btc * (COMMISSION_PCT * 2)
    net_pl      = raw_pl - comm
    notional    = entry_price * qty_btc
    net_pl_pct  = (net_pl / notional * 100) if notional > 0 else 0.0
    return {
        "raw_pl_usdt"    : raw_pl,
        "comm_usdt"      : comm,
        "commission_usdt": comm,        # alias used in telegram.notify_exit
        "net_pl_usdt"    : net_pl,
        "qty_btc"        : qty_btc,
        "lots"           : qty_lots,    # alias used in telegram.notify_exit
        "price_move"     : price_move,  # signed pts (+ = profit direction)
        "net_pl_pct"     : net_pl_pct,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskLevels:
    """SL / TP / stop_dist anchored to entry price."""
    entry_price: float
    sl:          float
    tp:          float
    stop_dist:   float
    atr:         float
    is_long:     bool
    is_trend:    bool


@dataclass
class TrailState:
    """Mutable trail state, updated on every tick by trail_loop."""
    stage:       int   = 0
    current_sl:  float = 0.0
    peak_price:  float = 0.0
    be_done:     bool  = False
    max_sl_fired: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# SL / TP calculation — exact Pine parity
# ─────────────────────────────────────────────────────────────────────────────

def calc_levels(
    entry_price: float,
    atr:         float,
    is_long:     bool,
    is_trend:    bool,
) -> RiskLevels:
    """
    Calculate SL and TP from entry_price using Pine Script formulas:

      rr       = trendRR   if is_trend else rangeRR
      atrMult  = trendATRmul if is_trend else rangeATRmul
      stopDist = min(atr * atrMult, maxSLPoints)
      longSL   = entryPrice - stopDist
      longTP   = entryPrice + stopDist * rr
    """
    rr       = TREND_RR       if is_trend else RANGE_RR
    atr_mult = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT

    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)

    if is_long:
        sl = entry_price - stop_dist
        tp = entry_price + stop_dist * rr
    else:
        sl = entry_price + stop_dist
        tp = entry_price - stop_dist * rr

    return RiskLevels(
        entry_price = entry_price,
        sl          = sl,
        tp          = tp,
        stop_dist   = stop_dist,
        atr         = atr,
        is_long     = is_long,
        is_trend    = is_trend,
    )


def recalc_levels_from_fill(risk: RiskLevels, fill_price: float) -> RiskLevels:
    """
    FIX-MAIN-1: Recalculate SL/TP from actual fill price.

    Pine Script: entryPrice = strategy.position_avg_price (fill)
                 longSL = entryPrice - stopDist
    The fill may differ from bar close due to slippage.
    Anchoring to fill_price matches Pine's position_avg_price behavior.
    """
    return calc_levels(fill_price, risk.atr, risk.is_long, risk.is_trend)


# ─────────────────────────────────────────────────────────────────────────────
# Trail helpers — must match Pine strategy.exit() exactly
# ─────────────────────────────────────────────────────────────────────────────

def get_trail_params(stage: int, atr: float) -> tuple[float, float]:
    """
    Return (active_pts, active_off) for the given stage.

    active_pts = trail activation distance (trail_points in Pine)
    active_off = trailing distance behind peak (trail_offset in Pine)

    When stage==0, uses stage-1 params (index 0) per Pine ternary chain.

    IMPORTANT for callers:
      Trail SL = peak_price - active_OFF   [long]   ← use index [1]
      Trail SL = peak_price + active_OFF   [short]  ← use index [1]
      NOT active_pts (index [0]) — that is only the ACTIVATION threshold.
    """
    idx = max(stage - 1, 0)
    _, pts_mult, off_mult = TRAIL_STAGES[idx]
    return atr * pts_mult, atr * off_mult


def calc_trail_stage(profit_dist: float, atr: float) -> int:
    """
    Return the highest stage whose trigger is satisfied.

    Pine:
      if trailStage < 5 and profitDist >= atr * trail5Trigger → 5
      elif trailStage < 4 ...
      ...

    profit_dist should be close-based (current close - entry) for paper
    trading parity with Pine, or tick-price-based for live trading.

    Stages only ever increase — caller must enforce: new_stage >= old_stage.
    """
    for i in range(len(TRAIL_STAGES) - 1, -1, -1):
        trigger_mult, _, _ = TRAIL_STAGES[i]
        if profit_dist >= atr * trigger_mult:
            return i + 1    # stages are 1-indexed
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Breakeven
# ─────────────────────────────────────────────────────────────────────────────

def should_trigger_be(profit_dist: float, atr: float) -> bool:
    """
    Pine: beTrigger = atr * beMult
          if close - entryPrice > beTrigger → move SL to entryPrice
    """
    return profit_dist > atr * BE_MULT


# ─────────────────────────────────────────────────────────────────────────────
# Max SL guard — BUG-RISK-002 FIX
# ─────────────────────────────────────────────────────────────────────────────

def max_sl_hit(
    current_price: float,
    entry_price:   float,
    atr:           float,
    is_long:       bool,
) -> bool:
    """
    Pine:
      maxSLdynPts = atr * maxSLMult      (2.0 ATR for 30M)
      maxSLPoints = 1500                  (30M-OPT-008)
      threshold   = min(maxSLdynPts, maxSLPoints)

      if long:  close <= entryPrice - threshold → fire Max SL
      if short: close >= entryPrice + threshold → fire Max SL

    BUG-RISK-002 FIX: old code used 1.5× / 500pt hardcodes.
    Now uses config MAX_SL_MULT (2.0) and MAX_SL_POINTS (1500).
    """
    threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    if is_long:
        return current_price <= entry_price - threshold
    else:
        return current_price >= entry_price + threshold


def max_sl_exit_price(entry_price: float, atr: float, is_long: bool) -> float:
    """
    The exit price used when Max SL fires.
    Pine closes at market, but for paper trading we estimate the fill
    at the threshold level.
    """
    threshold = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    return (entry_price - threshold) if is_long else (entry_price + threshold)


# ─────────────────────────────────────────────────────────────────────────────
# P/L calculation — exact Pine commission formula
# ─────────────────────────────────────────────────────────────────────────────

def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int,
) -> float:
    """
    Pine:
      rawPL = isLong ? (exitPx - entryPx) * qty : (entryPx - exitPx) * qty
      comm  = (entryPx + exitPx) * qty * 0.001   (0.05% each side = 0.1% round-trip)
      realPL = rawPL - comm
    """
    raw_pl = (exit_price - entry_price) * qty if is_long \
             else (entry_price - exit_price) * qty
    comm   = (entry_price + exit_price) * qty * (COMMISSION_PCT * 2)
    return raw_pl - comm
