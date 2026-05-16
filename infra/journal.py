"""
infra/journal_patch.py — Shiva Sniper v10
──────────────────────────────────────────────────────────────────────
PATCH for infra/journal.py:
  1. Adds `points_captured` column to trades table
  2. Refactors `log_trade()` to compute P&L via risk.lot_sizing
     (matches Delta-TransactionLog-OrderHistory.csv exactly:
        P&L USD = Points × Qty × 0.001)
  3. `get_trades()` now returns points_captured in the response
──────────────────────────────────────────────────────────────────────
APPLY:
  - Replace the DDL_TRADES / DDL_TRADES_SQLITE constants
  - Replace log_trade() and get_trades() with versions below
  - Run the one-time ALTER TABLE migration at startup (idempotent)
──────────────────────────────────────────────────────────────────────
"""

from risk.lot_sizing import compute_pnl_usd, compute_points


# ── Updated DDL (new column: points_captured) ────────────────────────────────
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


# ── Idempotent migration: call this from _init_db() AFTER the CREATE TABLE ──
def migrate_add_points_column(self) -> None:
    """ALTER TABLE on existing installs — safe to run every startup."""
    try:
        if self._driver == "postgres":
            self._execute("""
                ALTER TABLE trades
                ADD COLUMN IF NOT EXISTS points_captured DOUBLE PRECISION NOT NULL DEFAULT 0
            """)
        else:
            cur = self._cursor()
            cur.execute("PRAGMA table_info(trades)")
            cols = {row[1] for row in cur.fetchall()}
            if "points_captured" not in cols:
                self._execute(
                    "ALTER TABLE trades ADD COLUMN points_captured REAL NOT NULL DEFAULT 0"
                )
    except Exception as e:
        # logger.error(f"migrate_add_points_column failed: {e}")
        pass


# ── Replacement: log_trade() ─────────────────────────────────────────────────
def log_trade(self, signal_type: str, is_long: bool,
              entry_price: float, exit_price: float,
              sl: float, tp: float, atr: float,
              qty: int, real_pl: float = None,
              exit_reason: str = "", trail_stage: int = 0) -> None:
    """
    Log a completed trade.

    `real_pl` is now OPTIONAL — if omitted (or None) it's recomputed using
    the verified Delta formula so console/file/DB/Sheets always agree:
        Points  = (exit - entry) if LONG else (entry - exit)
        P&L USD = Points × Qty × 0.001
    """
    points  = compute_points(entry_price, exit_price, is_long)
    real_pl = compute_pnl_usd(entry_price, exit_price, qty, is_long) \
              if real_pl is None else round(real_pl, 4)

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
        # logger.info — keep the existing import in the real file
        import logging
        logging.getLogger("infra.journal").info(
            f"Trade logged [{self._driver}] | "
            f"{signal_type} {'LONG' if is_long else 'SHORT'} "
            f"qty={qty} lots  "
            f"entry={entry_price:.2f} exit={exit_price:.2f} "
            f"points={points:+.2f}  P/L={real_pl:+.4f} USD  "
            f"reason={exit_reason}"
        )
    except Exception as e:
        import logging
        logging.getLogger("infra.journal").error(f"log_trade failed: {e}")

    # Google Sheets sync — pass new fields through
    try:
        self._gsheet.log_trade(
            signal_type=signal_type, is_long=is_long,
            entry_price=entry_price, exit_price=exit_price,
            sl=sl, tp=tp, atr=atr,
            qty=qty,
            points_captured=points,
            real_pl=real_pl,
            exit_reason=exit_reason,
            trail_stage=trail_stage,
        )
    except Exception as e:
        import logging
        logging.getLogger("infra.journal").error(
            f"GSheet sync failed (trade still saved to DB): {e}"
        )


# ── Replacement: get_trades() (now returns points_captured) ──────────────────
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
        import logging
        logging.getLogger("infra.journal").error(f"get_trades failed: {e}")
        return []
