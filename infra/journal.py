"""
infra/journal.py
Persistent trade journal for Shiva Sniper v6.5.

UPGRADE: Now supports PostgreSQL (Supabase / any Postgres provider) as the
primary store, with SQLite as a local fallback when DATABASE_URL is not set.

WHY: SQLite at /app/journal.db on Render is wiped on every deploy — all trade
history is lost. PostgreSQL is external and persists forever.

SETUP (one-time):
  1. Create a free Supabase project at https://supabase.com
  2. Go to Settings -> Database -> Connection string (URI mode)
  3. Copy the URI: postgresql://postgres:[password]@db.[ref].supabase.co:5432/postgres
  4. Add to Render env vars: DATABASE_URL=<that URI>

Tables created automatically on first run:
  - trades      : one row per completed trade
  - open_trades : current live position (max 1 row at a time)
  - bot_events  : start / stop / error events for uptime tracking
"""

import os
import logging
import sqlite3
from datetime import datetime, timezone
from config import LOG_FILE

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Driver selection ──────────────────────────────────────────────────────────
def _get_driver():
    """Return 'postgres' if DATABASE_URL is set, else 'sqlite'."""
    return "postgres" if DATABASE_URL else "sqlite"


# ── SQL statements (paramstyle differs: %s for psycopg2, ? for sqlite3) ──────
def _ph(driver: str) -> str:
    """Return the correct placeholder char for the active driver."""
    return "%s" if driver == "postgres" else "?"


# ── DDL ───────────────────────────────────────────────────────────────────────
DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id           SERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL,
    signal_type  TEXT        NOT NULL,
    is_long      BOOLEAN     NOT NULL,
    entry_price  DOUBLE PRECISION NOT NULL,
    exit_price   DOUBLE PRECISION NOT NULL,
    sl           DOUBLE PRECISION NOT NULL,
    tp           DOUBLE PRECISION NOT NULL,
    atr          DOUBLE PRECISION NOT NULL,
    qty          INTEGER     NOT NULL,
    real_pl      DOUBLE PRECISION NOT NULL,
    exit_reason  TEXT        NOT NULL,
    trail_stage  INTEGER     NOT NULL
)
"""

DDL_TRADES_SQLITE = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    signal_type TEXT    NOT NULL,
    is_long     INTEGER NOT NULL,
    entry_price REAL    NOT NULL,
    exit_price  REAL    NOT NULL,
    sl          REAL    NOT NULL,
    tp          REAL    NOT NULL,
    atr         REAL    NOT NULL,
    qty         INTEGER NOT NULL,
    real_pl     REAL    NOT NULL,
    exit_reason TEXT    NOT NULL,
    trail_stage INTEGER NOT NULL
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
    """
    Unified trade journal.
    Connects to PostgreSQL if DATABASE_URL is set, SQLite otherwise.
    All public methods are identical regardless of backend.
    """

    def __init__(self):
        self._driver = _get_driver()
        self._conn   = None
        self._connect()
        self._init_db()
        logger.info(f"Journal initialised [{self._driver}]")

    # ── Connection ────────────────────────────────────────────────────
    def _connect(self) -> None:
        if self._driver == "postgres":
            try:
                import psycopg2
                self._conn = psycopg2.connect(DATABASE_URL)
                self._conn.autocommit = False
                logger.info("Connected to PostgreSQL")
            except Exception as e:
                logger.error(
                    f"PostgreSQL connection failed: {e} "
                    f"-- falling back to SQLite at {LOG_FILE}"
                )
                self._driver = "sqlite"
                self._conn = sqlite3.connect(LOG_FILE, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(LOG_FILE, check_same_thread=False)
            logger.info(f"Connected to SQLite at {LOG_FILE}")

    def _cursor(self):
        return self._conn.cursor()

    def _commit(self) -> None:
        self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        cur = self._cursor()
        cur.execute(sql, params)
        self._commit()

    # ── Schema ────────────────────────────────────────────────────────
    def _init_db(self) -> None:
        if self._driver == "postgres":
            for ddl in [DDL_TRADES, DDL_OPEN_TRADES, DDL_BOT_EVENTS]:
                self._execute(ddl)
        else:
            for ddl in [DDL_TRADES_SQLITE, DDL_OPEN_TRADES_SQLITE, DDL_BOT_EVENTS_SQLITE]:
                self._execute(ddl)

    # ── Helpers ───────────────────────────────────────────────────────
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ph(self) -> str:
        return _ph(self._driver)

    # ── Public API ────────────────────────────────────────────────────

    def log_trade(self, signal_type: str, is_long: bool,
                  entry_price: float, exit_price: float,
                  sl: float, tp: float, atr: float,
                  qty: int, real_pl: float,
                  exit_reason: str, trail_stage: int) -> None:
        """Log a completed trade to the trades table."""
        p = self._ph()
        sql = f"""
            INSERT INTO trades
            (ts, signal_type, is_long, entry_price, exit_price,
             sl, tp, atr, qty, real_pl, exit_reason, trail_stage)
            VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
        """
        try:
            self._execute(sql, (
                self._now(), signal_type, bool(is_long),
                entry_price, exit_price, sl, tp, atr,
                qty, real_pl, exit_reason, trail_stage,
            ))
            logger.info(
                f"Trade logged [{self._driver}] | "
                f"{signal_type} {'LONG' if is_long else 'SHORT'} "
                f"entry={entry_price:.2f} exit={exit_price:.2f} "
                f"P/L={real_pl:+.2f} USDT reason={exit_reason}"
            )
        except Exception as e:
            logger.error(f"log_trade failed: {e}")

    def open_trade(self, signal_type: str, is_long: bool,
                   entry_price: float, sl: float, tp: float,
                   atr: float, qty: int) -> None:
        """
        Record that a position was just opened.
        Clears any stale open_trades rows first (should be max 1).
        peak_price initialised to entry_price — updated each trail tick.
        """
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
                entry_price, sl, tp, atr, qty, sl, entry_price,  # FIX R3: peak_price = entry_price at open
            ))
            logger.info(f"Open trade recorded | {signal_type} entry={entry_price:.2f}")
        except Exception as e:
            logger.error(f"open_trade failed: {e}")

    def update_open_trade(self, trail_stage: int, current_sl: float,
                          peak_price: float = None) -> None:
        """Update trail stage, current SL, and peak_price on the live open trade."""
        p = self._ph()
        try:
            if peak_price is not None:
                # FIX R3: persist peak_price so recovery restores correct trail stage
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
        """Remove the open trade row when position is closed."""
        try:
            self._execute("DELETE FROM open_trades")
            logger.info("Open trade cleared from DB")
        except Exception as e:
            logger.error(f"close_open_trade failed: {e}")

    def log_event(self, event: str, detail: str = "") -> None:
        """Log a bot lifecycle event (start / stop / error)."""
        p = self._ph()
        try:
            self._execute(
                f"INSERT INTO bot_events (ts, event, detail) VALUES ({p},{p},{p})",
                (self._now(), event, detail),
            )
        except Exception as e:
            logger.error(f"log_event failed: {e}")

    def get_summary(self) -> dict:
        """Return quick P/L summary — used for Telegram /stats command."""
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
        """
        Return the current open position from DB, or None if no open trade.
        Used by main.py on startup to recover position after a redeploy.

        Returns dict with keys:
            signal_type, is_long, entry_price, sl, tp, atr, qty,
            trail_stage, current_sl
        """
        try:
            cur = self._cursor()
            cur.execute("""
                SELECT signal_type, is_long, entry_price, sl, tp,
                       atr, qty, trail_stage, current_sl, peak_price
                FROM open_trades
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return None
            keys = ["signal_type", "is_long", "entry_price", "sl", "tp",
                    "atr", "qty", "trail_stage", "current_sl", "peak_price"]  # FIX R3
            return dict(zip(keys, row))
        except Exception as e:
            logger.error(f"get_open_trade failed: {e}")
            return None

    def get_trades(self, limit: int = 50) -> list:
        """Return recent trades for the dashboard."""
        try:
            cur = self._cursor()
            cur.execute(f"""
                SELECT ts, signal_type, is_long, entry_price, exit_price,
                       sl, tp, atr, qty, real_pl, exit_reason, trail_stage
                FROM trades
                ORDER BY id DESC
                LIMIT {self._ph()}
            """, (limit,))
            rows = cur.fetchall()
            keys = ["ts", "signal_type", "is_long", "entry_price", "exit_price",
                    "sl", "tp", "atr", "qty", "real_pl", "exit_reason", "trail_stage"]
            return [dict(zip(keys, row)) for row in rows]
        except Exception as e:
            logger.error(f"get_trades failed: {e}")
            return []

    def close(self) -> None:
        if self._conn:
            self._conn.close()
