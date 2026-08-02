"""
risk/lot_sizing.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
Delta Exchange India BTCUSD perpetual contract sizing.

CONTRACT SPEC (verified against Delta-TransactionLog-OrderHistory.csv):
    1 Lot = 0.001 BTC face value  →  0.1 BTC = 100 Lots
    P&L (USD) = Points × Qty × 0.001  (inverse-style, $1 face per lot)

EXAMPLES:
    btc_to_lots(0.001) → 1
    btc_to_lots(0.05)  → 50
    btc_to_lots(0.1)   → 100
    btc_to_lots(1.0)   → 1000
──────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# Delta Exchange BTCUSD contract spec
BTC_PER_LOT       = 0.001          # 1 lot = 0.001 BTC face value
USD_PER_POINT_LOT = 0.001          # P&L = points × qty × 0.001 USD
MIN_LOTS          = 1
MAX_LOTS          = 1_000_000      # sanity ceiling


def btc_to_lots(btc_size: float) -> int:
    """
    Convert intended BTC position size → Delta lots (contracts).

    Rule:  0.1 BTC = 100 Lots  ⇒  lots = btc_size / 0.001 = btc_size × 1000
    Always rounds to the nearest integer lot; clamps to [1, 1_000_000].

    Raises ValueError if btc_size <= 0.
    """
    if btc_size is None or btc_size <= 0:
        raise ValueError(f"btc_size must be > 0, got {btc_size!r}")

    raw_lots = btc_size / BTC_PER_LOT
    lots     = int(round(raw_lots))
    lots     = max(MIN_LOTS, min(MAX_LOTS, lots))

    if abs(raw_lots - lots) > 1e-6:
        logger.warning(
            f"btc_to_lots: {btc_size} BTC = {raw_lots:.4f} lots → rounded to {lots}"
        )
    return lots


def lots_to_btc(qty_lots: int) -> float:
    """Inverse — useful for logging/display."""
    return qty_lots * BTC_PER_LOT


def compute_pnl_usd(entry: float, exit_price: float, qty_lots: int,
                    is_long: bool) -> float:
    """
    Exact Delta P&L formula (verified against CSV transaction log):
        Points = (exit - entry) if LONG else (entry - exit)
        P&L USD = Points × qty × 0.001

    Returns the realised P&L in USD before fees.
    """
    points = (exit_price - entry) if is_long else (entry - exit_price)
    return round(points * qty_lots * USD_PER_POINT_LOT, 4)


def compute_points(entry: float, exit_price: float, is_long: bool) -> float:
    """Raw price points captured (positive = profit, negative = loss)."""
    return round((exit_price - entry) if is_long else (entry - exit_price), 2)
