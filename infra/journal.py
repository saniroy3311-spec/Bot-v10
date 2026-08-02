"""
infra/journal.py — Shiva Sniper v10
Persistent trade journal.

v10 CHANGES:
  • NEW column `points_captured` in trades table (idempotent migration)
  • log_trade() recomputes Points + P&L from risk.lot_sizing every time,
    so console + DB + Sheets all carry identical numbers
        Points  = (exit - entry) if LONG else (entry - exit)
        P&L USD = Points × qty × 0.001   (matches Delta CSV exactly)
  • get_trades() now returns points_captured for the dashboard

FIXES KEPT FROM EARLIER VERSIONS:
  FIX-DB-001 | timeout=10 on sqlite3.connect (lock retry on rapid restart)
  FIX-DB-002 | WAL mode + busy_timeout=10000 (concurrent r/w)

BACKENDS:
  1. PostgreSQL (Supabase) — primary when DATABASE_URL is set
  2. SQLite — local fallback
"""

import os
import logging
import sqlite3
from datetime import datetime, timezone

from config           import LOG_FILE
from infra.gsheet     import GSheet
from risk.lot_sizing  import compute_pnl_usd, compute_points

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _get_driver():
    return "postgres" if DATABASE_URL else "sqlite"


# ── DDL ───────────────────────────────────────────────────────────────────────
DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    signal_type     TEXT        NOT NULL,
    is_long         BOOLEAN     NOT NULL,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION NOT NULL,
    sl              DOUBLE PRECISION NOT NULL,
    tp              DOUBLE PRECISION NOT NULL,
    atr             DOUBLE PRECISION NOT NULL,
    qty             INTEGER     NOT NULL,
    points_captured DOUBLE PRECISION NOT NULL DEFAULT 0,
    real_pl         DOUBLE PRECISION NOT NULL,
    exit_reason     TEXT        NOT NULL,
    trail_stage     INTEGER     NOT NULL
)
"""

DDL_TRADES_SQLITE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    signal_type     TEXT    NOT NULL,
    is_long         INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    sl              REAL    NOT NULL,
    tp              REAL    NOT NULL,
    atr             REAL    NOT NULL,
    qty             INTEGER NOT NULL,
    points_captured REAL    NOT NULL DEFAULT 0,
    real_pl         REAL    NOT NULL,
    exit_reason     TEXT    NOT NULL,
    trail_stage     INTEGER NOT NULL
)
"""

DDL_OPEN_TRADES = """
CREATE TABLE IF NOT EXISTS open_trades (
    id           SERIAL PRIMARY KEY,
    opened_at    TIMESTAMPTZ NOT NULL,
    signal_type  TEXT        NOT NULL,
    is_long      BOOLEAN     NOT NULL,
    entry_price  DOUBLE PRECISION NOT NULL,
    sl           DOUBLE PRECISION NOT NULL,
    tp           DOUBLE PRECISION NOT NULL,
    atr          DOUBLE PRECISION NOT NULL,
    qty          INTEGER     NOT NULL,
    trail_stage  INTEGER     NOT NULL DEFAULT 0,
    current_sl   DOUBLE PRECISION NOT NULL,
    peak_price   DOUBLE PRECISION NOT NULL DEFAULT 0
)
"""

DDL_OPEN_TRADES_SQLITE = """
CREATE TABLE IF NOT EXISTS open_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at   TEXT    NOT NULL,
    signal_type TEXT    NOT NULL,
    is_long     INTEGER NOT NULL,
    entry_price REAL    NOT NULL,
    sl          REAL    NOT NULL,
    tp          REAL    NOT NULL,
    atr         REAL    NOT NULL,
    qty         INTEGER NOT NULL,
    trail_stage INTEGER NOT NULL DEFAULT 0,
    current_sl  REAL    NOT NULL,
    peak_price  REAL    NOT NULL DEFAULT 0
)
"""

DDL_BOT_EVENTS = """
CREATE TABLE IF NOT EXISTS bot_events (
    id         SERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL,
    event      TEXT        NOT NULL,
    detail     TEXT
)
"""

DDL_BOT_EVENTS_SQLITE = """
CREATE TABLE IF NOT EXISTS bot_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    event  TEXT NOT NULL,
    detail TEXT
)
"""


class Journal:
    def __init__(self):
        self._driver = _get_driver()
        self._conn   = None
        self._gsheet = GSheet()
        self._connect()
        self._init_db()
        logger.info(
            f"Journal initialised [{self._driver}] | "
            f"GSheet={'enabled' if self._gsheet.enabled else 'disabled'}"
        )

    def _connect(self) -> None:
        if self._driver == "postgres":
            try:
                import psycopg2
                self._conn = psycopg2.connect(DATABASE_URL)
                self._conn.autocommit = False
                logger.info("Connected to PostgreSQL (Supabase)")
            except Exception as e:
                logger.error(
                    f"PostgreSQL connection failed: {e} "
                    f"-- falling back to SQLite at {LOG_FILE}"
                )
                self._driver = "sqlite"
                self._conn = sqlite3.connect(LOG_FILE, check_same_thread=False, timeout=10)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=10000")
        else:
            self._conn = sqlite3.connect(LOG_FILE, check_same_thread=False, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            logger.info(f"Connected to SQLite at {LOG_FILE}")

    def _cursor(self):
        return self._conn.cursor()

    def _commit(self) -> None:
        self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        cur = self._cursor()
        cur.execute(sql, params)
        self._commit()

    def _init_db(self) -> None:
        if self._driver == "postgres":
            for ddl in [DDL_TRADES, DDL_OPEN_TRADES, DDL_BOT_EVENTS]:
                self._execute(ddl)
        else:
            for ddl in [DDL_TRADES_SQLITE, DDL_OPEN_TRADES_SQLITE, DDL_BOT_EVENTS_SQLITE]:
                self._execute(ddl)
        self._migrate_add_points_column()

    def _migrate_add_points_column(self) -> None:
        """ALTER TABLE on existing installs — safe to run every startup."""
        try:
            if self._driver == "postgres":
                self._execute(
                    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS "
                    "points_captured DOUBLE PRECISION NOT NULL DEFAULT 0"
                )
            else:
                cur = self._cursor()
                cur.execute("PRAGMA table_info(trades)")
                cols = {row[1] for row in cur.fetchall()}
                if "points_captured" not in cols:
                    self._execute(
                        "ALTER TABLE trades ADD COLUMN "
                        "points_captured REAL NOT NULL DEFAULT 0"
                    )
                    logger.info("Migration: added trades.points_captured column")
        except Exception as e:
            logger.error(f"_migrate_add_points_column failed: {e}")

    def _ph(self) -> str:
        return "%s" if self._driver == "postgres" else "?"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── Trade I/O ─────────────────────────────────────────────────────────────

    def log_trade(self, signal_type: str, is_long: bool,
                  entry_price: float, exit_price: float,
                  sl: float, tp: float, atr: float,
                  qty: int, real_pl: float = None,
                  exit_reason: str = "", trail_stage: int = 0) -> None:
        """
        Log a completed trade.

        `real_pl` is OPTIONAL — if not provided, computed from
        risk.lot_sizing (matches Delta CSV exactly).
        """
        points  = compute_points(entry_price, exit_price, is_long)
        real_pl = (compute_pnl_usd(entry_price, exit_price, qty, is_long)
                   if real_pl is None else round(real_pl, 4))

        p = self._ph()
        sql = f"""
            INSERT INTO trades
            (ts, signal_type, is_long, entry_price, exit_price,
             sl, tp, atr, qty, points_captured, real_pl, exit_reason, trail_stage)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
        """
        try:
            self._execute(sql, (
                self._now(), signal_type, bool(is_long),
                entry_price, exit_price, sl, tp, atr,
                qty, points, real_pl, exit_reason, trail_stage,
            ))
            logger.info(
                f"Trade logged [{self._driver}] | "
                f"{signal_type} {'LONG' if is_long else 'SHORT'}  "
                f"qty={qty} lots  "
                f"entry={entry_price:.2f} exit={exit_price:.2f}  "
                f"points={points:+.2f}  P/L={real_pl:+.4f} USD  "
                f"reason={exit_reason}"
            )
        except Exception as e:
            logger.error(f"log_trade failed: {e}")

        # Google Sheets sync — non-blocking
        try:
            self._gsheet.log_trade(
                signal_type=signal_type, is_long=is_long,
                entry_price=entry_price, exit_price=exit_price,
                sl=sl, tp=tp, atr=atr,
                qty=qty,
                real_pl=real_pl,
                exit_reason=exit_reason,
                trail_stage=trail_stage,
                points_captured=points,
            )
        except Exception as e:
            logger.error(f"GSheet sync failed (trade still saved to DB): {e}")

    def open_trade(self, signal_type: str, is_long: bool,
                   entry_price: float, sl: float, tp: float,
                   atr: float, qty: int) -> None:
        p = self._ph()
        try:
            self._execute("DELETE FROM open_trades")
            sql = f"""
                INSERT INTO open_trades
                (opened_at, signal_type, is_long, entry_price,
                 sl, tp, atr, qty, trail_stage, current_sl, peak_price)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},0,{p},{p})
            """
            self._execute(sql, (
                self._now(), signal_type, bool(is_long),
                entry_price, sl, tp, atr, qty, sl, entry_price,
            ))
            logger.info(
                f"Open trade recorded | {signal_type}  qty={qty} lots  "
                f"entry={entry_price:.2f}"
            )
        except Exception as e:
            logger.error(f"open_trade failed: {e}")

    def update_open_trade(self, trail_stage: int, current_sl: float,
                          peak_price: float = None) -> None:
        p = self._ph()
        try:
            if peak_price is not None:
                self._execute(
                    f"UPDATE open_trades SET trail_stage={p}, current_sl={p}, peak_price={p}",
                    (trail_stage, current_sl, peak_price),
                )
            else:
                self._execute(
                    f"UPDATE open_trades SET trail_stage={p}, current_sl={p}",
                    (trail_stage, current_sl),
                )
        except Exception as e:
            logger.error(f"update_open_trade failed: {e}")

    def close_open_trade(self) -> None:
        try:
            self._execute("DELETE FROM open_trades")
            logger.info("Open trade cleared from DB")
        except Exception as e:
            logger.error(f"close_open_trade failed: {e}")

    def log_event(self, event: str, detail: str = "") -> None:
        p = self._ph()
        try:
            self._execute(
                f"INSERT INTO bot_events (ts, event, detail) VALUES ({p},{p},{p})",
                (self._now(), event, detail),
            )
        except Exception as e:
            logger.error(f"log_event failed: {e}")

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get_daily_summary(self, date_str: str = None) -> dict:
        """Return trade stats for a single calendar day (IST, YYYY-MM-DD)."""
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        if date_str is None:
            date_str = datetime.now(IST).strftime("%Y-%m-%d")
        try:
            cur = self._cursor()
            p   = self._ph()
            if self._driver == "postgres":
                cur.execute(f"""
                    SELECT
                        COUNT(*),
                        SUM(CASE WHEN real_pl > 0 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN real_pl < 0 THEN 1 ELSE 0 END),
                        COALESCE(SUM(real_pl), 0),
                        COALESCE(MAX(real_pl), 0),
                        COALESCE(MIN(real_pl), 0)
                    FROM trades
                    WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date = {p}::date
                """, (date_str,))
            else:
                cur.execute(f"""
                    SELECT
                        COUNT(*),
                        SUM(CASE WHEN real_pl > 0 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN real_pl < 0 THEN 1 ELSE 0 END),
                        COALESCE(SUM(real_pl), 0),
                        COALESCE(MAX(real_pl), 0),
                        COALESCE(MIN(real_pl), 0)
                    FROM trades
                    WHERE date(datetime(ts, '+5 hours', '+30 minutes')) = {p}
                """, (date_str,))
            row = cur.fetchone()
            total, wins, losses, total_pl, best, worst = row
            return {
                "date"    : date_str,
                "total"   : total    or 0,
                "wins"    : wins     or 0,
                "losses"  : losses   or 0,
                "total_pl": round(float(total_pl or 0), 4),
                "best"    : round(float(best     or 0), 4),
                "worst"   : round(float(worst    or 0), 4),
                "win_rate": round((wins / total * 100), 1) if total else 0.0,
            }
        except Exception as e:
            logger.error(f"get_daily_summary failed: {e}")
            return {}

    def get_summary(self) -> dict:
        try:
            cur = self._cursor()
            cur.execute("""
                SELECT
                    COUNT(*)                          AS total,
                    SUM(CASE WHEN real_pl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN real_pl < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(real_pl), 0)         AS total_pl,
                    COALESCE(MAX(real_pl), 0)         AS best,
                    COALESCE(MIN(real_pl), 0)         AS worst
                FROM trades
            """)
            row = cur.fetchone()
            total, wins, losses, total_pl, best, worst = row
            return {
                "total"   : total    or 0,
                "wins"    : wins     or 0,
                "losses"  : losses   or 0,
                "total_pl": total_pl or 0.0,
                "best"    : best     or 0.0,
                "worst"   : worst    or 0.0,
                "win_rate": (wins / total * 100) if total else 0.0,
            }
        except Exception as e:
            logger.error(f"get_summary failed: {e}")
            return {}

    def get_open_trade(self) -> dict | None:
        try:
            cur = self._cursor()
            cur.execute("""
                SELECT signal_type, is_long, entry_price, sl, tp,
                       atr, qty, trail_stage, current_sl, peak_price,
                       opened_at
                FROM open_trades
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            keys = ["signal_type", "is_long", "entry_price", "sl", "tp",
                    "atr", "qty", "trail_stage", "current_sl", "peak_price",
                    "opened_at"]
            return dict(zip(keys, row))
        except Exception as e:
            logger.error(f"get_open_trade failed: {e}")
            return None

    def get_trades(self, limit: int = 50) -> list:
        try:
            cur = self._cursor()
            cur.execute(f"""
                SELECT ts, signal_type, is_long, entry_price, exit_price,
                       sl, tp, atr, qty, points_captured,
                       real_pl, exit_reason, trail_stage
                FROM trades
                ORDER BY id DESC
                LIMIT {self._ph()}
            """, (limit,))
            rows = cur.fetchall()
            keys = ["ts", "signal_type", "is_long", "entry_price", "exit_price",
                    "sl", "tp", "atr", "qty", "points_captured",
                    "real_pl", "exit_reason", "trail_stage"]
            return [dict(zip(keys, row)) for row in rows]
        except Exception as e:
            logger.error(f"get_trades failed: {e}")
            return []

    def close(self) -> None:
        if self._conn:
            self._conn.close()
