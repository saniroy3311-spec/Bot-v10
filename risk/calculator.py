"""
risk/calculator.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

SL calculation matches Pine Script exactly:
    stopDist = math.min(atr * atrMultActive, maxSLPoints)
    Trend: atrMultActive = 0.9  → ~281 pts at ATR=312
    Range: atrMultActive = 0.7  → ~219 pts at ATR=312

    longSL  = entryPrice - stopDist
    longTP  = entryPrice + stopDist * rrActive
    shortSL = entryPrice + stopDist
    shortTP = entryPrice - stopDist * rrActive

recalc_levels_from_fill(): used ONLY in startup recovery path.

calc_real_pl(): Delta Exchange inverse-perp formula (verified vs CSV):
    Gross P&L (USD) = Points × qty × 0.001
    Points = (exit - entry) for LONG, (entry - exit) for SHORT
    Commission = entry_price × qty × 0.001 × COMMISSION_PCT
    Net P&L = Gross - Commission

    Example (your 2026-05-16 trade):
        Entry 78788, Exit 78685.50, SHORT, 1 lot
        Points  = 78788 - 78685.50 = 102.50
        Gross   = 102.50 × 1 × 0.001 = $0.1025 USD
        Comm    = 78788 × 1 × 0.001 × 0.00059 = $0.04649 USD  (but shown as gross only)
        Net     = $0.0560 USD

══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    TREND_ATR_MULT, RANGE_ATR_MULT,
    TREND_RR, RANGE_RR,
    MAX_SL_POINTS,
    COMMISSION_PCT,
)

# Delta inverse-perp multiplier: 1 lot = $1 face = 0.001 BTC at $1000 effective
_LOT_MULTIPLIER = 0.001


# ─── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class RiskLevels:
    """
    Immutable snapshot of SL / TP levels for one trade.

    entry_price — actual fill price
    sl          — initial stop loss  (entry ± ATR × atr_mult)
    tp          — take-profit price  (entry ∓ stopDist × R:R)
    stop_dist   — abs distance from entry to SL (pts)
    atr         — entry-bar ATR (used for Max SL and trail math)
    is_long     — True = long, False = short
    is_trend    — True = trend regime, False = range regime
    """
    entry_price:     float
    sl:              float
    tp:              float
    stop_dist:       float
    atr:             float
    is_long:         bool
    is_trend:        bool
    entry_bar_open:  float = 0.0


@dataclass
class TrailState:
    """
    Mutable per-trade trailing stop state.

    stage        — current trail stage (0 = no trail yet, 1–5 active)
    current_sl   — live stop loss level
    peak_price   — best price seen since entry (high for long, low for short)
    be_done      — True once breakeven activated (once per trade)
    max_sl_fired — True once Max SL circuit breaker fired
    """
    stage:         int   = 0
    current_sl:    float = 0.0
    peak_price:    float = 0.0
    be_done:       bool  = False
    max_sl_fired:  bool  = False


# ─── Core helpers ──────────────────────────────────────────────────────────────

def calc_levels(
    entry_price:    float,
    atr:            float,
    is_long:        bool,
    is_trend:       bool,
    entry_bar_open: float = 0.0,
) -> RiskLevels:
    """
    Compute initial SL and TP — Pine-exact formula.

    Pine Script:
        atrMultActive = isTrend ? trendATRmul : rangeATRmul
        stopDist      = math.min(atr * atrMultActive, maxSLPoints)
        longSL        = entryPrice - stopDist
        longTP        = entryPrice + stopDist * rrActive
        shortSL       = entryPrice + stopDist
        shortTP       = entryPrice - stopDist * rrActive
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
        entry_price    = entry_price,
        sl             = sl,
        tp             = tp,
        stop_dist      = stop_dist,
        atr            = atr,
        is_long        = is_long,
        is_trend       = is_trend,
        entry_bar_open = entry_bar_open,
    )


def recalc_levels_from_fill(risk: RiskLevels, fill_price: float) -> RiskLevels:
    """
    Shift SL / TP by the fill-vs-signal-close difference.
    Used ONLY in the startup recovery path — NOT for new live entries.
    """
    delta = fill_price - risk.entry_price
    return RiskLevels(
        entry_price    = fill_price,
        sl             = risk.sl  + delta,
        tp             = risk.tp  + delta,
        stop_dist      = risk.stop_dist,
        atr            = risk.atr,
        is_long        = risk.is_long,
        is_trend       = risk.is_trend,
        entry_bar_open = risk.entry_bar_open,
    )


def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int,
) -> float:
    """
    Delta Exchange inverse-perp P&L — verified against Delta CSV.

    Formula:
        points  = (exit - entry) if LONG else (entry - exit)
        gross   = points × qty × 0.001          (1 lot = $1 face = 0.001 BTC)
        comm    = entry × qty × 0.001 × COMMISSION_PCT
        net_pl  = gross - comm

    Verified example from Delta-TransactionLog-OrderHistory.csv:
        Entry=78788, Exit=78685.50, SHORT, qty=1
        points = 78788 - 78685.50 = 102.50
        gross  = 102.50 × 1 × 0.001 = 0.1025 USD  ✓
    """
    points = (
        (exit_price - entry_price) if is_long
        else (entry_price - exit_price)
    )
    gross = points * qty * _LOT_MULTIPLIER
    comm  = entry_price * qty * _LOT_MULTIPLIER * COMMISSION_PCT
    return round(gross - comm, 6)


def calc_gross_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int,
) -> float:
    """
    Gross P&L without commission.  Used by Telegram / dashboard display.
    gross = points × qty × 0.001
    """
    points = (
        (exit_price - entry_price) if is_long
        else (entry_price - exit_price)
    )
    return round(points * qty * _LOT_MULTIPLIER, 6)


def lots_to_btc(lots: int, price: float = 0.0) -> float:
    """
    Legacy back-compat signature (price arg unused in v10).
    v10 code should prefer risk.lot_sizing.lots_to_btc(lots).
    1 lot = 0.001 BTC face value on Delta inverse perp.
    """
    return lots * _LOT_MULTIPLIER


def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    qty:         int,
    is_long:     bool,
) -> dict:
    """
    Full breakdown used by gsheet.py and any legacy callers.

    Returns keys (both new and legacy):
        points_captured  — raw price move (direction-adjusted)
        qty_btc          — position size in BTC face value
        gross_pl_usdt    — points × qty × 0.001 (before commission)
        commission_usdt  — taker commission on entry leg
        net_pl_usdt      — gross - commission
        net_pl_pct       — net / (entry × qty_btc) × 100
        # legacy short-key aliases:
        raw_pl           — same as gross_pl_usdt
        commission       — same as commission_usdt
        net_pl           — same as net_pl_usdt
        price_move       — same as points_captured
    """
    points   = (exit_price - entry_price) if is_long else (entry_price - exit_price)
    qty_btc  = qty * _LOT_MULTIPLIER
    gross    = points * qty * _LOT_MULTIPLIER
    comm     = entry_price * qty_btc * COMMISSION_PCT
    net      = gross - comm
    pct      = (net / (entry_price * qty_btc) * 100) if qty_btc > 0 else 0.0

    return {
        # Primary keys
        "points_captured" : round(points,  4),
        "qty_btc"         : round(qty_btc, 6),
        "gross_pl_usdt"   : round(gross,   6),
        "commission_usdt" : round(comm,    6),
        "net_pl_usdt"     : round(net,     6),
        "net_pl_pct"      : round(pct,     4),
        # Legacy aliases (gsheet used these names)
        "raw_pl"          : round(gross,   6),
        "commission"      : round(comm,    6),
        "net_pl"          : round(net,     6),
        "price_move"      : round(points,  4),
        "raw_pl_usdt"     : round(gross,   6),
    }
