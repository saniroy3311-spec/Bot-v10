"""
infra/gsheet.py
Google Sheets integration for Shiva Sniper v11.

PURPOSE:
  Every completed trade is appended to a Google Sheet in real-time.
  The sheet also has a Dashboard tab with summary formulas.

SETUP (one-time):
  1. Go to https://console.cloud.google.com
  2. Create a project → Enable "Google Sheets API" + "Google Drive API"
  3. Create a Service Account → download JSON key file
  4. Share your Google Sheet with the service account email (Editor)
  5. Set env vars on Hostinger VPS:
       GSHEET_CREDENTIALS_JSON = <paste the full JSON key content as one line>
       GSHEET_SPREADSHEET_ID   = <the long ID from the sheet URL>

SHEET STRUCTURE:
  Tab "Trades"    — one row per closed trade (auto-appended)
  Tab "Dashboard" — summary stats (auto-created with formulas on first run)

COLUMNS in Trades tab:
  A: Timestamp (IST)
  B: Signal Type
  C: Direction (LONG/SHORT)
  D: Entry Price
  E: Exit Price
  F: Price Move
  G: Lots
  H: BTC Qty
  I: SL
  J: TP
  K: ATR
  L: Gross P/L (USDT)
  M: Commission (USDT)
  N: Net P/L (USDT)
  O: Return %
  P: Exit Reason
  Q: Trail Stage
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# IST = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

# Column headers for Trades sheet
TRADE_HEADERS = [
    "Timestamp (IST)", "Signal Type", "Direction",
    "Entry Price", "Exit Price", "Price Move (pts)",
    "Lots", "BTC Qty", "SL", "TP", "ATR",
    "Gross P/L (USDT)", "Commission (USDT)", "Net P/L (USDT)",
    "Return %", "Exit Reason", "Trail Stage",
]


def _load_creds():
    """Load Google service account credentials from env var."""
    raw = os.environ.get("GSHEET_CREDENTIALS_JSON", "")
    if not raw:
        raise ValueError(
            "GSHEET_CREDENTIALS_JSON env var is not set. "
            "See infra/gsheet.py header for setup instructions."
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"GSHEET_CREDENTIALS_JSON is not valid JSON: {e}")


class GSheet:
    """
    Google Sheets writer.
    Uses gspread (lightweight wrapper around Sheets API v4).
    """

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
                "GSheet disabled — set GSHEET_CREDENTIALS_JSON + GSHEET_SPREADSHEET_ID to enable."
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
        """Create Trades and Dashboard tabs if they don't exist."""
        existing = [ws.title for ws in self._sh.worksheets()]

        # ── Trades tab ────────────────────────────────────────────────────────
        if "Trades" not in existing:
            trades_ws = self._sh.add_worksheet(title="Trades", rows=5000, cols=20)
            trades_ws.append_row(TRADE_HEADERS, value_input_option="RAW")
            # Freeze header row
            trades_ws.freeze(rows=1)
            logger.info("Created 'Trades' tab with headers")
        else:
            trades_ws = self._sh.worksheet("Trades")
            # Check if headers exist
            first_row = trades_ws.row_values(1)
            if not first_row:
                trades_ws.append_row(TRADE_HEADERS, value_input_option="RAW")
                trades_ws.freeze(rows=1)

        # ── Dashboard tab ─────────────────────────────────────────────────────
        if "Dashboard" not in existing:
            dash_ws = self._sh.add_worksheet(title="Dashboard", rows=50, cols=10)
            self._write_dashboard_formulas(dash_ws)
            logger.info("Created 'Dashboard' tab with formulas")

    def _write_dashboard_formulas(self, ws):
        """Write summary formulas into the Dashboard tab."""
        rows = [
            ["🤖 Shiva Sniper — Trade Dashboard", ""],
            ["", ""],
            ["📊 SUMMARY", "Value"],
            ["Total Trades",           "=COUNTA(Trades!A2:A)-1"],
            ["Wins",                   "=COUNTIF(Trades!N2:N,\">0\")"],
            ["Losses",                 "=COUNTIF(Trades!N2:N,\"<0\")"],
            ["Win Rate %",             "=IFERROR(D5/D4*100,0)"],
            ["", ""],
            ["💰 P/L SUMMARY", ""],
            ["Total Net P/L (USDT)",   "=SUM(Trades!N2:N)"],
            ["Best Trade (USDT)",      "=MAX(Trades!N2:N)"],
            ["Worst Trade (USDT)",     "=MIN(Trades!N2:N)"],
            ["Avg Win (USDT)",         "=AVERAGEIF(Trades!N2:N,\">0\")"],
            ["Avg Loss (USDT)",        "=AVERAGEIF(Trades!N2:N,\"<0\")"],
            ["Total Commission (USDT)","=SUM(Trades!M2:M)"],
            ["", ""],
            ["📏 SIZE STATS", ""],
            ["Total Lots Traded",      "=SUM(Trades!G2:G)"],
            ["Total BTC Traded",       "=SUM(Trades!H2:H)"],
            ["", ""],
            ["🏆 EXIT BREAKDOWN", ""],
            ["Trail SL exits",         "=COUNTIF(Trades!P2:P,\"Trail*\")"],
            ["Bracket TP exits",       "=COUNTIF(Trades!P2:P,\"Bracket-TP\")"],
            ["Bracket SL exits",       "=COUNTIF(Trades!P2:P,\"Bracket-SL\")"],
            ["", ""],
            ["🕐 Last updated",        "=MAX(Trades!A2:A)"],
        ]
        for i, row in enumerate(rows, start=1):
            ws.update(f"A{i}:B{i}", [row])

    def log_trade(
        self,
        signal_type: str,
        is_long:     bool,
        entry_price: float,
        exit_price:  float,
        sl:          float,
        tp:          float,
        atr:         float,
        qty:         int,
        real_pl:     float,
        exit_reason: str,
        trail_stage: int,
    ) -> bool:
        """
        Append one completed trade row to the Trades tab.
        Returns True on success, False on failure.
        """
        if not self._enabled:
            return True  # silently skip if not configured

        try:
            self._connect()
            from risk.calculator import lots_to_btc, calc_pl_breakdown
            plb       = calc_pl_breakdown(entry_price, exit_price, is_long, qty)
            ts_ist    = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
            direction = "LONG" if is_long else "SHORT"

            row = [
                ts_ist,
                signal_type,
                direction,
                round(entry_price, 2),
                round(exit_price, 2),
                round(plb["price_move"], 2),
                qty,
                round(plb["qty_btc"], 4),
                round(sl, 2),
                round(tp, 2),
                round(atr, 2),
                round(plb["raw_pl_usdt"], 4),
                round(plb["commission_usdt"], 4),
                round(plb["net_pl_usdt"], 4),
                round(plb["net_pl_pct"], 4),
                exit_reason,
                trail_stage,
            ]

            trades_ws = self._sh.worksheet("Trades")
            trades_ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info(
                f"GSheet: trade logged | {signal_type} {direction} "
                f"entry={entry_price:.2f} exit={exit_price:.2f} "
                f"net={plb['net_pl_usdt']:+.4f} USDT"
            )
            return True

        except Exception as e:
            logger.error(f"GSheet log_trade failed: {e}")
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled
