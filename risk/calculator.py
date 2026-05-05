"""
risk/calculator.py — Shiva Sniper Bot-v10
══════════════════════════════════════════════════════════════════════════════

Shared dataclasses and pure-math helpers imported by:
  • indicators/engine.py   (calc_levels)
  • monitor/trail_loop.py  (RiskLevels, TrailState)
  • main.py                (everything)

All values mirror Pine Script v6 exactly — see shiva_sniper_report.pdf for
the full derivation.

Pine parity notes
─────────────────
• stop_dist = min(ATR × atr_mult, MAX_SL_POINTS)
    → Trend  atr_mult = 0.9   (trendATRmul)
    → Range  atr_mult = 0.7   (rangeATRmul)
• TP = entry ± stop_dist × R:R
    → Trend  R:R = 5.0   (trendRR)
    → Range  R:R = 3.0   (rangeRR)
• recalc_levels_from_fill(): used ONLY in startup recovery path.
  For new entries, SL/TP are anchored to signal-bar close (Pine parity).
  See PINE-PARITY-SL note in main.py.
• calc_real_pl(): 0.059% taker on entry, 0% maker on exit — mirrors
  Pine's commission_value = 0.059 setting.
══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    Immutable (or near-immutable) snapshot of SL / TP levels for one trade.

    entry_price — actual fill price (updated after fill; SL/TP stay pinned
                  to signal-bar close for Pine parity)
    sl          — initial stop loss price
    tp          — take-profit price
    stop_dist   — abs distance from entry to initial SL (points)
    atr         — entry-bar ATR (used for initial SL/TP and Max SL only)
    is_long     — True = long, False = short
    is_trend    — True = trend regime, False = range regime
    """
    entry_price: float
    sl:          float
    tp:          float
    stop_dist:   float
    atr:         float
    is_long:     bool
    is_trend:    bool


@dataclass
class TrailState:
    """
    Mutable per-trade trailing stop state.

    stage       — current trail stage (0 = no trail active yet, 1-5 active)
    current_sl  — the live stop loss level (starts at initial SL, improves
                  as trail/BE advance it)
    peak_price  — highest high (long) or lowest low (short) seen since entry
    be_done     — True once breakeven has been activated (fires once per trade)
    max_sl_fired — True once the Max SL circuit breaker has fired
    """
    stage:         int   = 0
    current_sl:    float = 0.0
    peak_price:    float = 0.0
    be_done:       bool  = False
    max_sl_fired:  bool  = False


# ─── Core helpers ──────────────────────────────────────────────────────────────

def calc_levels(
    entry_price: float,
    atr:         float,
    is_long:     bool,
    is_trend:    bool,
) -> RiskLevels:
    """
    Compute initial SL and TP from entry price + ATR.

    Mirrors Pine:
        stopDist = math.min(atr * atrMultActive, maxSLPoints)
        longSL   = entryPrice - stopDist
        longTP   = entryPrice + stopDist * rrActive
        shortSL  = entryPrice + stopDist
        shortTP  = entryPrice - stopDist * rrActive
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
    Shift SL / TP by the difference between signal-bar close and actual fill.

    Used ONLY in the startup recovery path (bot restart mid-trade) to
    re-anchor levels to the fill price when the original signal-bar close
    is no longer available.

    NOTE: Do NOT call this for new live entries — main.py pins SL/TP to
    the signal-bar close for Pine parity (PINE-PARITY-SL).
    """
    delta = fill_price - risk.entry_price
    return RiskLevels(
        entry_price = fill_price,
        sl          = risk.sl  + delta,
        tp          = risk.tp  + delta,
        stop_dist   = risk.stop_dist,
        atr         = risk.atr,
        is_long     = risk.is_long,
        is_trend    = risk.is_trend,
    )


def calc_real_pl(
    entry_price: float,
    exit_price:  float,
    is_long:     bool,
    qty:         int,
) -> float:
    """
    Commission-adjusted P&L.

    Mirrors Pine's calcRealPL():
        rawPL = (exitPx - entryPx) * qty            (long)
              = (entryPx - exitPx) * qty            (short)
        comm  = entryPx * qty * 0.00059             (0.059% taker entry only)
        return rawPL - comm

    Exit is assumed maker (0% fee) matching Pine's default.
    """
    raw_pl = (
        (exit_price - entry_price) * qty if is_long
        else (entry_price - exit_price) * qty
    )
    comm = entry_price * qty * COMMISSION_PCT
    return raw_pl - comm
