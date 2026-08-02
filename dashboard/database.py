import os
from datetime import datetime

TURSO_URL   = os.environ.get("TURSO_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN", "")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DB = os.path.join(_REPO_ROOT, "journal.db")
CLIENTS_DB = os.path.join(_REPO_ROOT, "clients.db")

def get_db_connection(db_name=None):
    if TURSO_URL and TURSO_TOKEN:
        import libsql_experimental as libsql
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        return conn
    else:
        import sqlite3
        conn = sqlite3.connect(db_name)
        conn.row_factory = sqlite3.Row
        return conn

def _exec(conn, query, params=()):
    cursor = conn.execute(query, params)
    conn.commit()
    return cursor

def init_databases():
    conn = get_db_connection()

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            qty INTEGER NOT NULL,
            points_captured REAL NOT NULL,
            gross_pnl REAL NOT NULL,
            fee REAL NOT NULL,
            net_pnl REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            tag TEXT
        )
    """)

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            qty_allocated INTEGER NOT NULL,
            capital REAL DEFAULT 0.0,
            start_date TEXT NOT NULL,
            status TEXT NOT NULL,
            profit_share REAL NOT NULL,
            fee_cap REAL,
            floor INTEGER DEFAULT 1,
            billing_cycle TEXT NOT NULL,
            currency TEXT DEFAULT 'USD',
            notes TEXT
        )
    """)

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            timestamp TEXT NOT NULL,
            change_description TEXT NOT NULL,
            FOREIGN KEY (client_id) REFERENCES clients (id)
        )
    """)

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT NOT NULL UNIQUE,
            client_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            period TEXT NOT NULL,
            trades_count INTEGER NOT NULL,
            gross_pnl REAL NOT NULL,
            fees REAL NOT NULL,
            net_pnl REAL NOT NULL,
            our_fee REAL NOT NULL,
            net_payout REAL NOT NULL,
            status TEXT NOT NULL,
            payment_method TEXT,
            issue_date TEXT NOT NULL,
            paid_date TEXT,
            notes TEXT,
            FOREIGN KEY (client_id) REFERENCES clients (id)
        )
    """)

    _exec(conn, """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        )
    """)

    cur = conn.execute("SELECT COUNT(*) FROM settings")
    if cur.fetchone()[0] == 0:
        defaults = [
            ("business_name", "The Greeks"),
            ("logo_url", ""),
            ("total_bot_lots", "100"),
            ("default_billing_cycle", "Monthly"),
            ("usd_inr_rate", "85.00"),
            ("whatsapp_webhook", ""),
            ("daily_drawdown_limit", "500"),
            ("heartbeat_timeout", "300"),
            ("timezone", "IST"),
        ]
        for key, val in defaults:
            _exec(conn, "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))

    conn.close()

