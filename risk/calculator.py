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
calc_real_pl(): 0.059% taker on entry, 0% maker on exit — mirrors Pine.
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
    Commission-adjusted P&L — mirrors Pine's calcRealPL().

    rawPL = (exitPx - entryPx) * qty   (long)
          = (entryPx - exitPx) * qty   (short)
    comm  = entryPx * qty * 0.00059    (0.059% taker entry, 0% maker exit)
    """
    raw_pl = (
        (exit_price - entry_price) * qty if is_long
        else (entry_price - exit_price) * qty
    )
    comm = entry_price * qty * COMMISSION_PCT
    return raw_pl - comm


def lots_to_btc(lots: int, price: float) -> float:
    """Delta BTCUSD inverse perp: 1 lot = 1 USD / price BTC."""
    if price <= 0:
        return 0.0
    return lots / price


def calc_pl_breakdown(
    entry_price: float,
    exit_price:  float,
    qty:         int,
    is_long:     bool,
) -> dict:
    """Return raw_pl, commission, net_pl. Used by gsheet.py."""
    raw_pl = (
        (exit_price - entry_price) * qty if is_long
        else (entry_price - exit_price) * qty
    )
    comm   = entry_price * qty * COMMISSION_PCT
    net_pl = raw_pl - comm
    return {"raw_pl": raw_pl, "commission": comm, "net_pl": net_pl}
