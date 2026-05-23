"""
infra/gsheet.py — Shiva Sniper v10
Google Sheets integration.

v10 CHANGES:
  • log_trade() now accepts `points_captured` kwarg (passed by Journal)
  • Computes P&L breakdown directly from risk.lot_sizing
    (matches Delta-TransactionLog-OrderHistory.csv exactly:
        Gross P/L = Points × Qty × 0.001 USDT
        Commission = entry_price × qty × 0.001 × COMMISSION_PCT
        Net P/L    = Gross − Commission)
  • New column R: Points Captured

FIXES SHIPPED IN THIS REWRITE:
  • Previous version called calc_pl_breakdown(entry, exit, is_long, qty)
    but the function signature is (entry, exit, qty, is_long) — args were
    swapped. Also read keys "price_move/qty_btc/raw_pl_usdt/..." that
    don't exist in calc_pl_breakdown. Both bugs caused every GSheet write
    to throw before reaching the network. Fixed by computing the
    breakdown locally from risk.lot_sizing.

SETUP (one-time):
  1. console.cloud.google.com → enable Sheets API + Drive API
  2. Create a Service Account → download JSON key
  3. Share the target sheet with the service account email (Editor)
  4. Set env vars:
       GSHEET_CREDENTIALS_JSON = <one-line JSON>
       GSHEET_SPREADSHEET_ID   = <ID from sheet URL>

COLUMNS in Trades tab:
  A: Timestamp (IST)        J: TP
  B: Signal Type            K: ATR
  C: Direction              L: Gross P/L (USDT)
  D: Entry Price            M: Commission (USDT)
  E: Exit Price             N: Net P/L (USDT)
  F: Price Move (pts)       O: Return %
  G: Lots                   P: Exit Reason
  H: BTC Qty                Q: Trail Stage
  I: SL                     R: Points Captured  ← NEW v10
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta

from config          import COMMISSION_PCT
from risk.lot_sizing import (
    BTC_PER_LOT, USD_PER_POINT_LOT,
    lots_to_btc, compute_points, compute_pnl_usd,
)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

TRADE_HEADERS = [
    "Timestamp (IST)", "Signal Type", "Direction",
    "Entry Price", "Exit Price", "Price Move (pts)",
    "Lots", "BTC Qty", "SL", "TP", "ATR",
    "Gross P/L (USDT)", "Commission (USDT)", "Net P/L (USDT)",
    "Return %", "Exit Reason", "Trail Stage",
    "Points Captured",   # v10
]


def _load_creds():
    raw = os.environ.get("GSHEET_CREDENTIALS_JSON", "")
    if not raw:
        raise ValueError(
            "GSHEET_CREDENTIALS_JSON env var is not set. "
            "See infra/gsheet.py header for setup."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GSHEET_CREDENTIALS_JSON is not valid JSON: {e}")


class GSheet:
    """Append-one-row-per-trade writer for Google Sheets."""

    def __init__(self):
        self._spreadsheet_id = os.environ.get("GSHEET_SPREADSHEET_ID", "")
        self._gc   = None
        self._sh   = None
        self._enabled = bool(
            os.environ.get("GSHEET_CREDENTIALS_JSON") and
            os.environ.get("GSHEET_SPREADSHEET_ID")
        )
        if not self._enabled:
            logger.info(
                "GSheet disabled — set GSHEET_CREDENTIALS_JSON + "
                "GSHEET_SPREADSHEET_ID to enable."
            )

    def _connect(self):
        """Lazy connect — only called when actually writing."""
        if self._gc is not None:
            return
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            creds_dict = _load_creds()
            scopes = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds    = Credentials.from_service_account_info(creds_dict, scopes=scopes)
            self._gc = gspread.authorize(creds)
            self._sh = self._gc.open_by_key(self._spreadsheet_id)
            logger.info(f"Connected to Google Sheet: {self._sh.title}")
            self._ensure_sheets()
        except Exception as e:
            logger.error(f"GSheet connection failed: {e}")
            raise

    def _ensure_sheets(self):
        existing = [ws.title for ws in self._sh.worksheets()]

        if "Trades" not in existing:
            trades_ws = self._sh.add_worksheet(title="Trades", rows=5000, cols=20)
            trades_ws.append_row(TRADE_HEADERS, value_input_option="RAW")
            trades_ws.freeze(rows=1)
            logger.info("Created 'Trades' tab with headers")
        else:
            trades_ws = self._sh.worksheet("Trades")
            first_row = trades_ws.row_values(1)
            if not first_row:
                trades_ws.append_row(TRADE_HEADERS, value_input_option="RAW")
                trades_ws.freeze(rows=1)
            elif "Points Captured" not in first_row:
                # v10: append the new column header if upgrading from older sheet
                col = len(first_row) + 1
                trades_ws.update_cell(1, col, "Points Captured")
                logger.info("Added 'Points Captured' header to existing Trades tab")

        if "Dashboard" not in existing:
            dash_ws = self._sh.add_worksheet(title="Dashboard", rows=50, cols=10)
            self._write_dashboard_formulas(dash_ws)
            logger.info("Created 'Dashboard' tab with formulas")

    def _write_dashboard_formulas(self, ws):
        rows = [
            ["🤖 Shiva Sniper — Trade Dashboard", ""],
            ["", ""],
            ["📊 SUMMARY", "Value"],
            ["Total Trades",            "=COUNTA(Trades!A2:A)-1"],
            ["Wins",                    "=COUNTIF(Trades!N2:N,\">0\")"],
            ["Losses",                  "=COUNTIF(Trades!N2:N,\"<0\")"],
            ["Win Rate %",              "=IFERROR(B5/B4*100,0)"],
            ["", ""],
            ["💰 P/L SUMMARY", ""],
            ["Total Net P/L (USDT)",    "=SUM(Trades!N2:N)"],
            ["Best Trade (USDT)",       "=MAX(Trades!N2:N)"],
            ["Worst Trade (USDT)",      "=MIN(Trades!N2:N)"],
            ["Avg Win (USDT)",          "=AVERAGEIF(Trades!N2:N,\">0\")"],
            ["Avg Loss (USDT)",         "=AVERAGEIF(Trades!N2:N,\"<0\")"],
            ["Total Commission (USDT)", "=SUM(Trades!M2:M)"],
            ["", ""],
            ["📏 SIZE STATS", ""],
            ["Total Lots Traded",       "=SUM(Trades!G2:G)"],
            ["Total BTC Traded",        "=SUM(Trades!H2:H)"],
            ["Total Points Captured",   "=SUM(Trades!R2:R)"],  # v10
            ["", ""],
            ["🏆 EXIT BREAKDOWN", ""],
            ["Trail SL exits",          "=COUNTIF(Trades!P2:P,\"Trail*\")"],
            ["Bracket TP exits",        "=COUNTIF(Trades!P2:P,\"Bracket-TP\")"],
            ["Bracket SL exits",        "=COUNTIF(Trades!P2:P,\"Bracket-SL\")"],
            ["", ""],
            ["🕐 Last updated",         "=MAX(Trades!A2:A)"],
        ]
        for i, row in enumerate(rows, start=1):
            ws.update(f"A{i}:B{i}", [row])

    # ── Internal: Delta P&L breakdown (replaces buggy risk.calculator call) ──
    @staticmethod
    def _pl_breakdown(entry_price: float, exit_price: float,
                      qty: int, is_long: bool) -> dict:
        """
        Delta inverse-perp BTCUSD math (matches Delta CSV exactly):
            Points    = (exit - entry) if LONG else (entry - exit)
            Qty BTC   = qty × 0.001
            Gross P/L = Points × qty × 0.001  USDT
            Commission= entry_price × qty_btc × COMMISSION_PCT  USDT
            Net P/L   = Gross − Commission
        """
        points       = compute_points(entry_price, exit_price, is_long)
        qty_btc      = lots_to_btc(qty)
        gross_pl     = compute_pnl_usd(entry_price, exit_price, qty, is_long)
        commission   = entry_price * qty_btc * COMMISSION_PCT
        net_pl       = gross_pl - commission
        # Return % is computed against margin proxy (entry × qty_btc)
        notional     = max(entry_price * qty_btc, 1e-9)
        net_pl_pct   = (net_pl / notional) * 100.0
        return {
            "points":     round(points, 2),
            "qty_btc":    round(qty_btc, 6),
            "gross_pl":   round(gross_pl, 6),
            "commission": round(commission, 6),
            "net_pl":     round(net_pl, 6),
            "net_pl_pct": round(net_pl_pct, 4),
        }

    def log_trade(
        self,
        signal_type:     str,
        is_long:         bool,
        entry_price:     float,
        exit_price:      float,
        sl:              float,
        tp:              float,
        atr:             float,
        qty:             int,
        real_pl:         float = None,
        exit_reason:     str   = "",
        trail_stage:     int   = 0,
        points_captured: float = None,        # v10 — accepted from Journal
    ) -> bool:
        """Append one closed-trade row. Returns True on success."""
        if not self._enabled:
            return True  # silently skip

        try:
            self._connect()
            if self._sh is None:  # FIX-BUG5: guard against failed _connect()
                logger.warning("GSheet not connected — skipping log_trade")
                return False

            plb       = self._pl_breakdown(entry_price, exit_price, qty, is_long)
            points    = points_captured if points_captured is not None else plb["points"]
            net_pl    = real_pl if real_pl is not None else plb["net_pl"]
            ts_ist    = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            direction = "LONG" if is_long else "SHORT"

            row = [
                ts_ist,
                signal_type,
                direction,
                round(entry_price, 2),
                round(exit_price, 2),
                round(plb["points"], 2),
                qty,
                plb["qty_btc"],
                round(sl, 2),
                round(tp, 2),
                round(atr, 2),
                plb["gross_pl"],
                plb["commission"],
                round(net_pl, 4),
                plb["net_pl_pct"],
                exit_reason,
                trail_stage,
                round(points, 2),                 # v10: column R
            ]

            trades_ws = self._sh.worksheet("Trades")
            trades_ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info(
                f"GSheet: trade logged | {signal_type} {direction} "
                f"entry={entry_price:.2f} exit={exit_price:.2f} "
                f"points={points:+.2f} net={net_pl:+.4f} USDT"
            )
            return True

        except Exception as e:
            logger.error(f"GSheet log_trade failed: {e}")
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled
