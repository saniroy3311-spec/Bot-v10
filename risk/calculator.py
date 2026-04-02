"""
risk/calculator.py
SL/TP calculation, 5-stage trail ratchet, breakeven, max SL guard.

CHANGES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-RISK-1 | SL and TP must be anchored to FILL price, not bar close.

  Root cause:
    Pine Script: entryPrice = strategy.position_avg_price (the actual fill).
                 longSL = entryPrice - stopDist
                 longTP = entryPrice + stopDist * rr
    So Pine's SL/TP are always measured from the real fill price.

    Old bot: calc_levels(snap.close, ...) anchored SL/TP to bar close.
             Then main.py updated entry_price = fill_price but left
             risk.sl and risk.tp pointing at the bar-close anchor.
             Result: if fill slipped 50 pts vs close, SL was also
             50 pts in the wrong place — wrong P/L, wrong exits.

  Fix: calc_levels() still uses the passed price as anchor (which
       main.py now passes as fill_price after the order fills).
       A new helper recalc_levels_from_fill() is provided so main.py
       can recalculate SL/TP with the actual fill price after entry.

FIX-RISK-2 | calc_real_pl() corrected for Delta India USD-margined contract.

  Delta India BTCUSD perpetual is USD-margined (not BTC-margined):
    P/L = (exit - entry) * qty_lots / entry   → USD value
    This is INVERSE perpetual math.

  Previous formula treated it as a linear contract:
    P/L = (exit - entry) * qty_btc
  which is WRONG for USD-margined inverse contracts.

  Correct formula (matches Delta India settlement):
    raw_pl = qty_lots * (1/entry - 1/exit)  × 1_000_000
           = qty_lots × (exit - entry) / (entry × exit) × 1_000_000

  For small price moves the difference is small, but at BTC prices of
  ~66000 it's ~0.07% error per trade — visible over many trades.

  NOTE: If the contract is actually USDT-margined (symbol BTCUSDT),
  use the linear formula. Check Delta India contract specs.
  Your .env SYMBOL=BTC/USD:USD → this is the inverse USD-margined contract.
──────────────────────────────────────────────────────────────────────────────

DELTA INDIA CONTRACT DETAILS (BTC/USD:USD):
  Contract type : Inverse perpetual (USD-margined)
  Contract size : 1 USD per lot
  1 lot         = 1 USD notional at entry price
  P/L in USD    = qty_lots × (1/entry_px - 1/exit_px)
                  [this is the standard inverse perpetual formula]
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

    Call this AFTER the fill is confirmed, passing fill_price as entry_price.
    This matches Pine Script exactly:
        entryPrice = strategy.position_avg_price  (the actual fill)
        longSL     = entryPrice - stopDist
        longTP     = entryPrice + stopDist * rr
    """
    atr_mult  = TREND_ATR_MULT if is_trend else RANGE_ATR_MULT
    rr        = TREND_RR       if is_trend else RANGE_RR
    stop_dist = min(atr * atr_mult, MAX_SL_POINTS)
    if stop_dist <= 0:
        stop_dist = atr * 0.5

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
    FIX-RISK-1: Recalculate SL/TP anchored to actual fill price.

    Called by main.py immediately after place_entry() returns the fill.
    Pine Script always anchors SL/TP to position_avg_price (the fill),
    not to the bar close. This function replicates that behaviour.

    Usage in main.py:
        order = await self.order_mgr.place_entry(...)
        fill_price = float(order.get("average") or order.get("price"))
        risk = recalc_levels_from_fill(risk, fill_price)
    """
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

def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int   = ALERT_QTY,
    commission:  float = COMMISSION_PCT,
) -> float:
    """
    FIX-RISK-2: Correct P/L for Delta India BTC/USD:USD inverse perpetual.

    Contract: USD-margined inverse perpetual.
    Contract size: 1 USD per lot.
    P/L formula:
        raw_pl = qty_lots × (1/entry - 1/exit)          [in BTC]
               = qty_lots × (exit - entry) / (entry × exit)  [in BTC]
        convert to USD: raw_pl_usd = raw_pl × exit_price   ← standard inverse PnL

    Simplified for USD output:
        raw_pl_usd = qty_lots × (exit - entry) / entry   (approx, small-move)

    Commission: Delta India taker = 0.05% of notional per side.
        notional_entry = qty_lots / entry_price  (in BTC) × entry_price = qty_lots USD
        commission     = 2 × qty_lots × 0.0005
    """
    if entry_price <= 0 or exit_price <= 0:
        return 0.0

    # Inverse perpetual P/L in USD
    # For long: profit when exit > entry
    # For short: profit when exit < entry
    if is_long:
        raw_pl = qty * (exit_price - entry_price) / entry_price
    else:
        raw_pl = qty * (entry_price - exit_price) / entry_price

    # Commission: 0.05% × notional per side × 2 sides
    # Notional in USD = qty lots (each lot = 1 USD)
    commission_usd = 2 * qty * commission

    return raw_pl - commission_usd


def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int   = ALERT_QTY,
    commission:  float = COMMISSION_PCT,
) -> dict:
    """Full breakdown dict for Telegram messages and logging."""
    if entry_price <= 0 or exit_price <= 0:
        return {
            "lots": qty, "price_move": 0, "raw_pl_usdt": 0,
            "commission_usdt": 0, "net_pl_usdt": 0, "net_pl_pct": 0,
        }

    move = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    # Inverse perpetual: raw P/L in USD
    raw_pl         = qty * move / entry_price
    commission_usd = 2 * qty * commission
    net_pl         = raw_pl - commission_usd
    pct            = (net_pl / qty * 100) if qty else 0.0

    return {
        "lots":            qty,
        "price_move":      round(move, 2),
        "raw_pl_usdt":     round(raw_pl, 4),
        "commission_usdt": round(commission_usd, 4),
        "net_pl_usdt":     round(net_pl, 4),
        "net_pl_pct":      round(pct, 4),
    }
