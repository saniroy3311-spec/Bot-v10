"""
risk/calculator.py
SL/TP calculation, 5-stage trail ratchet, breakeven, max SL guard.

Mirrors Shiva Sniper v6.5 Pine Script risk engine exactly.

CONFIG MAPPING (from config.py):
  TRAIL_STAGES[i] = (trigger_mult, points_mult, offset_mult)
    trigger_mult : profit must reach  atr * trigger_mult  to activate stage
    points_mult  : SL distance from peak  = atr * points_mult
    offset_mult  : bracket limit buffer   = atr * offset_mult  (passed to modify_sl)

  BE_MULT      : breakeven triggers when profit_dist >= atr * BE_MULT
  MAX_SL_MULT  : emergency close when loss >= atr * MAX_SL_MULT
  MAX_SL_POINTS: hard cap on loss in price points (whichever is smaller)
"""

from dataclasses import dataclass, field
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


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class RiskLevels:
    """SL/TP levels computed at entry."""
    entry_price: float
    sl:          float
    tp:          float
    stop_dist:   float   # distance from entry to initial SL (positive)
    atr:         float   # ATR value at entry bar — frozen here for trail use
    is_long:     bool
    is_trend:    bool


@dataclass
class TrailState:
    """Mutable trail/BE state — lives in memory while position is open."""
    stage:       int   = 0
    current_sl:  float = 0.0
    peak_price:  float = 0.0
    be_done:     bool  = False
    max_sl_fired: bool = False


# ── Entry-level calculations ───────────────────────────────────────────────────

def calc_levels(
    close:    float,
    atr:      float,
    is_long:  bool,
    is_trend: bool,
) -> RiskLevels:
    """
    Compute SL, TP, and stop distance for a new entry.

    Stop distance:
      Trend: atr * TREND_ATR_MULT, capped at MAX_SL_POINTS
      Range: atr * RANGE_ATR_MULT, capped at MAX_SL_POINTS

    TP:
      Long:  entry + stop_dist * RR
      Short: entry - stop_dist * RR

    SL:
      Long:  entry - stop_dist
      Short: entry + stop_dist
    """
    atr_mult = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr       = TREND_RR       if is_trend else RANGE_RR

    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    if stop_dist <= 0:
        stop_dist = atr * 0.5   # safety fallback

    if is_long:
        sl = close - stop_dist
        tp = close + stop_dist * rr
    else:
        sl = close + stop_dist
        tp = close - stop_dist * rr

    return RiskLevels(
        entry_price=close,
        sl=sl,
        tp=tp,
        stop_dist=stop_dist,
        atr=atr,
        is_long=is_long,
        is_trend=is_trend,
    )


# ── Trail engine ───────────────────────────────────────────────────────────────

def calc_trail_stage(profit_dist: float, atr: float) -> int:
    """
    Return the highest trail stage whose trigger threshold has been crossed.

    TRAIL_STAGES[i] = (trigger_mult, points_mult, offset_mult)
    Stage i activates when profit_dist >= atr * trigger_mult[i].

    Returns 0 if no stage triggered.
    """
    stage = 0
    for i, (trigger_mult, _, _) in enumerate(TRAIL_STAGES):
        if profit_dist >= atr * trigger_mult:
            stage = i + 1
        else:
            break
    return stage


def get_trail_params(stage: int, atr: float) -> Tuple[float, float]:
    """
    Return (trail_pts, trail_off) for a given trail stage.

    trail_pts = atr * points_mult  → SL distance from peak price
    trail_off = atr * offset_mult  → limit order buffer (passed to modify_sl)

    Args:
        stage: 1–5 (must be >= 1)
        atr:   entry-bar ATR (frozen in RiskLevels)

    Returns:
        (trail_pts, trail_off)
    """
    if stage < 1 or stage > len(TRAIL_STAGES):
        raise ValueError(f"trail stage must be 1–{len(TRAIL_STAGES)}, got {stage}")
    _, points_mult, offset_mult = TRAIL_STAGES[stage - 1]
    return atr * points_mult, atr * offset_mult


# ── BE + max SL guards ─────────────────────────────────────────────────────────

def should_trigger_be(profit_dist: float, atr: float) -> bool:
    """
    Returns True when profit has moved far enough to move SL to breakeven.
    Pine Script: breakeven triggers at atr * BE_MULT in profit.
    """
    return profit_dist >= atr * BE_MULT


def max_sl_hit(
    current_price: float,
    entry_price:   float,
    atr:           float,
    is_long:       bool,
) -> bool:
    """
    Returns True if the position has lost more than the hard max-loss cap.

    Cap is the SMALLER of:
      atr * MAX_SL_MULT  (ATR-relative cap)
      MAX_SL_POINTS      (absolute point cap)

    This prevents catastrophic loss on gap moves where the bracket SL is
    not filled at the expected level.
    """
    loss_cap = min(atr * MAX_SL_MULT, MAX_SL_POINTS)
    if is_long:
        return current_price <= entry_price - loss_cap
    else:
        return current_price >= entry_price + loss_cap


# ── P/L calculation ────────────────────────────────────────────────────────────

def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int   = ALERT_QTY,
    commission:  float = COMMISSION_PCT,
) -> float:
    """
    Compute realised P/L after commission (both legs).

    P/L = raw_move * qty - (entry_value + exit_value) * commission_per_side * 2
    For perpetual futures (USDT-margined), move is in USDT per contract.

    Args:
        entry_price: Fill price at entry.
        exit_price:  Fill price at exit.
        is_long:     True for long, False for short.
        qty:         Number of contracts.
        commission:  Per-side rate (e.g. 0.0005 = 0.05%).

    Returns:
        Net P/L in USDT (positive = profit).
    """
    if is_long:
        raw_pl = (exit_price - entry_price) * qty
    else:
        raw_pl = (entry_price - exit_price) * qty

    # Commission on both entry and exit legs
    comm = (entry_price + exit_price) * qty * commission
    return raw_pl - comm
