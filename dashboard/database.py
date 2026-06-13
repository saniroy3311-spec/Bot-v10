import sqlite3
import os
from datetime import datetime, timedelta
import random

# Resolve paths relative to repo root (parent of this dashboard/ directory)
_REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DB  = os.path.join(_REPO_ROOT, "journal.db")   # bot's live trade log
CLIENTS_DB  = os.path.join(_REPO_ROOT, "clients.db")   # dashboard billing DB

def get_db_connection(db_name):
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn

def init_databases():
    # 1. Initialize journal.db (Bot trades database)
    conn_journal = get_db_connection(JOURNAL_DB)
    cursor_journal = conn_journal.cursor()
    cursor_journal.execute("""
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
    conn_journal.commit()
    
    # Check if trades table is empty; if so, populate mock data
    cursor_journal.execute("SELECT COUNT(*) FROM trades")
    if cursor_journal.fetchone()[0] == 0:
        #seed_mock_trades(cursor_journal)
        conn_journal.commit()
    conn_journal.close()

    # 2. Initialize clients.db (Dashboard configuration, settings, billing, client allocations)
    conn_clients = get_db_connection(CLIENTS_DB)
    cursor_clients = conn_clients.cursor()
    cursor_clients.execute("""
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
    
    # Migration: Add capital column if it does not exist in an existing DB
    try:
        cursor_clients.execute("ALTER TABLE clients ADD COLUMN capital REAL DEFAULT 0.0")
        conn_clients.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    cursor_clients.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER,
            timestamp TEXT NOT NULL,
            change_description TEXT NOT NULL,
            FOREIGN KEY (client_id) REFERENCES clients (id)
        )
    """)

    cursor_clients.execute("""
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

    cursor_clients.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        )
    """)
    conn_clients.commit()

    # Check if settings table is empty, seed defaults
    cursor_clients.execute("SELECT COUNT(*) FROM settings")
    if cursor_clients.fetchone()[0] == 0:
        default_settings = [
            ("business_name", "The Greeks"),
            ("logo_url", ""),
            ("total_bot_lots", "100"),
            ("default_billing_cycle", "Monthly"),
            ("usd_inr_rate", "85.00"),
            ("whatsapp_webhook", "https://api.whatsapp.com/send?phone=1234567"),
            ("daily_drawdown_limit", "500"),
            ("heartbeat_timeout", "120"),
            ("timezone", "IST")
        ]
        cursor_clients.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", default_settings)
        conn_clients.commit()

    # Reset business_name to "The Greeks"
    cursor_clients.execute("UPDATE settings SET value = 'The Greeks' WHERE key = 'business_name'")
    conn_clients.commit()

    # Check if clients table is empty, seed mock clients
    cursor_clients.execute("SELECT COUNT(*) FROM clients")
    if cursor_clients.fetchone()[0] == 0:
        mock_clients = [
            ("Rahul Sharma", 70, 70000.0, "2026-05-01", "Active", 60.0, None, 1, "Monthly", "USD", "Primary VIP client"),
            ("Nikhil Verma", 20, 20000.0, "2026-05-15", "Active", 70.0, 500.0, 0, "Weekly", "INR", "Prefers local payment conversion")
        ]
        for name, qty, cap, s_date, status, p_share, f_cap, floor, cycle, currency, notes in mock_clients:
            cursor_clients.execute("""
                INSERT INTO clients (name, qty_allocated, capital, start_date, status, profit_share, fee_cap, floor, billing_cycle, currency, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, qty, cap, s_date, status, p_share, f_cap, floor, cycle, currency, notes))
            client_id = cursor_clients.lastrowid
            
            # Log initial audit entry
            cursor_clients.execute("""
                INSERT INTO audit_logs (client_id, timestamp, change_description)
                VALUES (?, ?, ?)
            """, (client_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Onboarded client with allocation of " + str(qty) + " lots and capital of $" + str(cap) + "."))
            
        conn_clients.commit()
        
        # Seed mock invoice
        mock_invoice = (
            "INV-2026-0001",
            1,
            "Rahul Sharma",
            "May 2026",
            34,
            3800.0,
            120.0,
            3680.0,
            1472.0, # 40% fee to provider
            2208.0, # 60% payout to client
            "Paid",
            "Crypto (USDT)",
            "2026-06-01 10:00:00",
            "2026-06-02 14:30:00",
            "Settled via TRC-20 wallet transaction."
        )
        cursor_clients.execute("""
            INSERT INTO invoices (invoice_number, client_id, client_name, period, trades_count, gross_pnl, fees, net_pnl, our_fee, net_payout, status, payment_method, issue_date, paid_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, mock_invoice)
        conn_clients.commit()

    conn_clients.close()

def seed_mock_trades(cursor):
    # Generates a series of trades over the last 40 days to create a beautiful equity curve
    start_time = datetime.now() - timedelta(days=40)
    current_time = start_time
    
    trade_count = 80
    random.seed(42) # For reproducible mock data
    
    price = 65000.0
    total_lots = 100
    lot_multiplier = 0.1 # 1 point movement per lot = $0.1
    
    for i in range(1, trade_count + 1):
        # Time progression
        current_time += timedelta(hours=random.randint(4, 20), minutes=random.randint(0, 59))
        
        side = "LONG" if random.random() > 0.45 else "SHORT"
        
        # Entry price drift
        price += random.randint(-1500, 1800)
        
        # Assign symbol and determine scale factor
        symbol = random.choice(["BTCUSD", "ETHUSD", "SOLUSD"])
        if symbol == "BTCUSD":
            factor = 1.0
        elif symbol == "ETHUSD":
            factor = 15.0
        else: # SOLUSD
            factor = 350.0
            
        entry_price = round(price / factor, 2)
        
        # Points captured: average win is positive to show a profitable system
        win = random.random() < 0.58
        if win:
            points_captured_raw = random.randint(80, 650)
            exit_reason = random.choice(["TP Hit", "Trail SL"])
        else:
            points_captured_raw = -random.randint(50, 300)
            exit_reason = random.choice(["Bracket SL", "Manual"])
            
        points_captured = round(points_captured_raw / factor, 2)
        exit_price = round(entry_price + points_captured if side == "LONG" else entry_price - points_captured, 2)
        
        # Re-verify points_captured to avoid float mismatch
        points_captured = round(exit_price - entry_price if side == "LONG" else entry_price - exit_price, 2)
        
        # Calculate P&Ls
        qty = total_lots
        gross_pnl = round(points_captured * qty * (lot_multiplier * factor), 2)
        
        # Standard fee base
        normal_fee = qty * 0.45 + abs(points_captured * factor) * 0.01
        
        # Scalper duration: BTC/ETH 30m, others 15m
        duration_minutes = random.randint(5, 120)
        
        is_scalper = False
        if symbol in ["BTCUSD", "ETHUSD"]:
            if duration_minutes <= 30:
                is_scalper = True
        else:
            if duration_minutes <= 15:
                is_scalper = True
                
        if is_scalper:
            # waive closing fee (zero closing fee leg => 50% discount)
            fee = round(normal_fee / 2.0, 2)
        else:
            fee = round(normal_fee, 2)
            
        net_pnl = round(gross_pnl - fee, 2)
        
        entry_dt = current_time
        exit_dt = entry_dt + timedelta(minutes=duration_minutes)
        
        tag = random.choice(["Slippage", "Regular", "News", "Gap Open", None, None])
        
        cursor.execute("""
            INSERT INTO trades (symbol, entry_time, exit_time, side, entry_price, exit_price, qty, points_captured, gross_pnl, fee, net_pnl, exit_reason, tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            entry_dt.strftime("%Y-%m-%d %H:%M:%S"),
            exit_dt.strftime("%Y-%m-%d %H:%M:%S"),
            side,
            entry_price,
            exit_price,
            qty,
            points_captured,
            gross_pnl,
            fee,
            net_pnl,
            exit_reason,
            tag
        ))
        
        # Update current time to after trade exit
        current_time = exit_dt

if __name__ == "__main__":
    init_databases()
    print("Databases initialized and seeded successfully.")
