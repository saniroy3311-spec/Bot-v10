from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import os
import json
import httpx
from datetime import datetime, timedelta
from typing import Optional, List
import database

# Resolve runtime file paths to repo root (parent of dashboard/)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _repo(filename: str) -> str:
    return os.path.join(_REPO_ROOT, filename)

app = FastAPI(title="Shiva Sniper Bot Dashboard API")

# Enable CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure databases are initialized on startup
@app.on_event("startup")
def startup_event():
    database.init_databases()

# Helper to fetch database connection dict rows
def get_db_rows(db_name, query, params=()):
    conn = database.get_db_connection(db_name)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def execute_db_write(db_name, query, params=()):
    conn = database.get_db_connection(db_name)
    cursor = conn.cursor()
    cursor.execute(query, params)
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id

# Pydantic Schemas
class ClientSchema(BaseModel):
    name: str
    qty_allocated: int
    capital: Optional[float] = 0.0
    start_date: str
    status: str
    profit_share: float
    fee_cap: Optional[float] = None
    floor: int = 1
    billing_cycle: str
    currency: str = "USD"
    notes: Optional[str] = ""

class SettingsUpdateSchema(BaseModel):
    business_name: str
    total_bot_lots: int
    default_billing_cycle: str
    usd_inr_rate: float
    whatsapp_webhook: str
    daily_drawdown_limit: float
    heartbeat_timeout: int

class InvoiceCreateSchema(BaseModel):
    client_id: int
    period: str
    trades_count: int
    gross_pnl: float
    fees: float
    net_pnl: float
    our_fee: float
    net_payout: float
    payment_method: str
    notes: Optional[str] = ""

# Serve frontend static files
@app.get("/", response_class=HTMLResponse)
def get_index():
    _here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(_here, "index.html")):
        return FileResponse(os.path.join(_here, "index.html"))
    return "<h3>index.html not found!</h3>"

@app.get("/styles.css")
def get_styles():
    _here = os.path.dirname(os.path.abspath(__file__))
    _path = os.path.join(_here, "styles.css")
    if os.path.exists(_path):
        return FileResponse(_path)
    return HTTPException(status_code=404, detail="styles.css not found")

@app.get("/the_greeks_logo.png")
def get_logo():
    _here = os.path.dirname(os.path.abspath(__file__))
    _path = os.path.join(_here, "the_greeks_logo.png")
    if os.path.exists(_path):
        return FileResponse(_path)
    return HTTPException(status_code=404, detail="Logo not found")

# --- API ENDPOINTS ---

# 1. Dashboard Health & Live Status
@app.get("/api/status")
async def get_status():
    # Read health.json written by the bot's main.py heartbeat
    health_data = {}
    health_path = _repo("health.json")
    
    # Try reading from default path
    if not os.path.exists(health_path):
        # Create a mock health file for local demo
        mock_health = {
            "timestamp": int(datetime.now().timestamp()),
            "cpu": 12 + int(datetime.now().second % 15),
            "ram": 38,
            "ws_delta": True,
            "last_tick_age_s": 1
        }
        with open(health_path, "w") as f:
            json.dump(mock_health, f)
            
    try:
        with open(health_path, "r") as f:
            health_data = json.load(f)
    except Exception:
        health_data = {"timestamp": 0, "cpu": 0, "ram": 0, "ws_delta": False, "last_tick_age_s": 999}

    # Fetch active settings
    settings_rows = get_db_rows(database.CLIENTS_DB, "SELECT key, value FROM settings")
    settings = {r["key"]: r["value"] for r in settings_rows}
    
    # Calculate bot uptime/status
    now_ts = int(datetime.now().timestamp())
    hb_timeout = int(settings.get("heartbeat_timeout", 120))
    
    bot_live = False
    status_text = "STOPPED"
    if health_data.get("timestamp", 0) > 0:
        age = now_ts - health_data["timestamp"]
        if age < hb_timeout:
            bot_live = True
            status_text = "LIVE"
        elif age < hb_timeout * 3:
            status_text = "ERROR"
        else:
            status_text = "STOPPED"
            
    # Try calling Delta Exchange REST API for BTC price/position
    # If it fails, we fall back to a reasonable simulated price and flat position.
    btc_price = 68450.00
    open_position = {"side": "FLAT", "entry_price": 0.0, "qty": 0, "unrealised_pnl": 0.0}
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get("https://api.delta.exchange/v2/tickers/BTCUSD", timeout=2.0)
            if res.status_code == 200:
                data = res.json()
                btc_price = float(data.get("result", {}).get("close", 68450.00))
    except Exception:
        pass # Keep mock price if api fails
        
    # We can fetch open position from a local file, or simulate it
    position_path = _repo("position.json")
    if os.path.exists(position_path):
        try:
            with open(position_path, "r") as f:
                open_position = json.load(f)
        except Exception:
            pass
    else:
        # Simulate small position for interactive demo
        minute = datetime.now().minute
        if minute % 10 < 3: # 30% of the time, show active position
            is_long = (minute % 2 == 0)
            side = "LONG" if is_long else "SHORT"
            entry_offset = -120 if is_long else 120
            entry_price = round(btc_price + entry_offset, 2)
            sl_offset = -250 if is_long else 250
            sl = round(entry_price + sl_offset, 2)
            tp_offset = 600 if is_long else -600
            tp = round(entry_price + tp_offset, 2)
            unrealised_pnl = round((btc_price - entry_price) * 10 * 0.1, 2) if is_long else round((entry_price - btc_price) * 10 * 0.1, 2)
            open_position = {
                "side": side,
                "is_long": is_long,
                "entry_price": entry_price,
                "qty": 10,
                "sl": sl,
                "current_sl": sl,
                "tp": tp,
                "trail_stage": (minute % 5) + 1,
                "signal_type": f"Trend {side.capitalize()}",
                "opened_at": (datetime.now() - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
                "unrealised_pnl": unrealised_pnl
            }
        else:
            open_position = {
                "side": "FLAT",
                "is_long": True,
                "entry_price": 0.0,
                "qty": 0,
                "sl": 0.0,
                "current_sl": 0.0,
                "tp": 0.0,
                "trail_stage": 0,
                "signal_type": "",
                "opened_at": "",
                "unrealised_pnl": 0.0
            }
            
    return {
        "bot_status": status_text,
        "last_heartbeat_ago": now_ts - health_data.get("timestamp", now_ts),
        "health": health_data,
        "btc_price": btc_price,
        "open_position": open_position,
        "settings": settings
    }

# 2. Trades API
@app.get("/api/trades")
def get_trades(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    side: Optional[str] = None,
    result: Optional[str] = None,
    min_pnl: Optional[float] = None,
    max_pnl: Optional[float] = None,
    min_points: Optional[float] = None,
    tag: Optional[str] = None
):
    query = "SELECT * FROM trades WHERE 1=1"
    params = []
    
    if start_date:
        query += " AND entry_time >= ?"
        params.append(start_date)
    if end_date:
        query += " AND exit_time <= ?"
        params.append(end_date)
    if side and side != "ALL":
        query += " AND side = ?"
        params.append(side)
    if result:
        if result == "WINNERS":
            query += " AND net_pnl > 0"
        elif result == "LOSERS":
            query += " AND net_pnl <= 0"
    if min_pnl is not None:
        query += " AND net_pnl >= ?"
        params.append(min_pnl)
    if max_pnl is not None:
        query += " AND net_pnl <= ?"
        params.append(max_pnl)
    if min_points is not None:
        query += " AND ABS(points_captured) >= ?"
        params.append(min_points)
    if tag:
        query += " AND tag LIKE ?"
        params.append(f"%{tag}%")
        
    query += " ORDER BY exit_time DESC"
    trades = get_db_rows(database.JOURNAL_DB, query, params)
    return trades

# 3. Client Management CRUD
@app.get("/api/clients")
def get_clients():
    return get_db_rows(database.CLIENTS_DB, "SELECT * FROM clients")

@app.post("/api/clients")
def create_client(client: ClientSchema):
    # Check total capacity allocation
    settings_rows = get_db_rows(database.CLIENTS_DB, "SELECT value FROM settings WHERE key='total_bot_lots'")
    total_capacity = int(settings_rows[0]["value"]) if settings_rows else 100
    
    allocated_rows = get_db_rows(database.CLIENTS_DB, "SELECT SUM(qty_allocated) as total FROM clients WHERE status IN ('Active', 'Owner')")
    allocated = allocated_rows[0]["total"] or 0
    
    if client.status in ("Active", "Owner") and (allocated + client.qty_allocated > total_capacity):
        raise HTTPException(status_code=400, detail=f"Capacity exceeded! Total lots: {total_capacity}. Currently allocated: {allocated}. Requested: {client.qty_allocated}.")

    client_id = execute_db_write(
        database.CLIENTS_DB,
        """
        INSERT INTO clients (name, qty_allocated, capital, start_date, status, profit_share, fee_cap, floor, billing_cycle, currency, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client.name, client.qty_allocated, client.capital, client.start_date, client.status, client.profit_share, client.fee_cap, client.floor, client.billing_cycle, client.currency, client.notes)
    )
    
    # Audit log
    execute_db_write(
        database.CLIENTS_DB,
        "INSERT INTO audit_logs (client_id, timestamp, change_description) VALUES (?, ?, ?)",
        (client_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"Client onboarding. Assigned {client.qty_allocated} lots and capital of ${client.capital}.")
    )
    return {"id": client_id, "message": "Client created successfully"}

@app.put("/api/clients/{client_id}")
def update_client(client_id: int, client: ClientSchema):
    # Check total capacity allocation (excluding current client if active)
    settings_rows = get_db_rows(database.CLIENTS_DB, "SELECT value FROM settings WHERE key='total_bot_lots'")
    total_capacity = int(settings_rows[0]["value"]) if settings_rows else 100
    
    allocated_rows = get_db_rows(database.CLIENTS_DB, "SELECT SUM(qty_allocated) as total FROM clients WHERE status IN ('Active', 'Owner') AND id != ?", (client_id,))
    allocated = allocated_rows[0]["total"] or 0
    
    if client.status in ("Active", "Owner") and (allocated + client.qty_allocated > total_capacity):
        raise HTTPException(status_code=400, detail="Capacity exceeded!")
 
    # Fetch original client for audit logging
    original = get_db_rows(database.CLIENTS_DB, "SELECT * FROM clients WHERE id = ?", (client_id,))
    if not original:
        raise HTTPException(status_code=404, detail="Client not found")
        
    orig = original[0]
    changes = []
    if orig["qty_allocated"] != client.qty_allocated:
        changes.append(f"Lots changed from {orig['qty_allocated']} to {client.qty_allocated}")
    if orig.get("capital", 0.0) != client.capital:
        changes.append(f"Capital changed from ${orig.get('capital', 0.0)} to ${client.capital}")
    if orig["status"] != client.status:
        changes.append(f"Status changed from {orig['status']} to {client.status}")
    if orig["profit_share"] != client.profit_share:
        changes.append(f"Profit share changed from {orig['profit_share']}% to {client.profit_share}%")
        
    execute_db_write(
        database.CLIENTS_DB,
        """
        UPDATE clients
        SET name=?, qty_allocated=?, capital=?, start_date=?, status=?, profit_share=?, fee_cap=?, floor=?, billing_cycle=?, currency=?, notes=?
        WHERE id=?
        """,
        (client.name, client.qty_allocated, client.capital, client.start_date, client.status, client.profit_share, client.fee_cap, client.floor, client.billing_cycle, client.currency, client.notes, client_id)
    )
    
    if changes:
        execute_db_write(
            database.CLIENTS_DB,
            "INSERT INTO audit_logs (client_id, timestamp, change_description) VALUES (?, ?, ?)",
            (client_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ", ".join(changes))
        )
        
    return {"message": "Client updated successfully"}

@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int):
    execute_db_write(database.CLIENTS_DB, "DELETE FROM clients WHERE id = ?", (client_id,))
    execute_db_write(database.CLIENTS_DB, "DELETE FROM audit_logs WHERE client_id = ?", (client_id,))
    return {"message": "Client deleted successfully"}

@app.get("/api/clients/{client_id}/audit")
def get_client_audit(client_id: int):
    return get_db_rows(database.CLIENTS_DB, "SELECT * FROM audit_logs WHERE client_id = ? ORDER BY timestamp DESC", (client_id,))

# 4. Invoices API
@app.get("/api/invoices")
def get_invoices():
    return get_db_rows(database.CLIENTS_DB, "SELECT * FROM invoices ORDER BY issue_date DESC")

@app.post("/api/invoices")
def create_invoice(invoice: InvoiceCreateSchema):
    issue_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Fetch client details for confirmation
    client = get_db_rows(database.CLIENTS_DB, "SELECT name FROM clients WHERE id = ?", (invoice.client_id,))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    client_name = client[0]["name"]
    
    # Generate automatic invoice number
    prefix = f"INV-{datetime.now().year}"
    count_rows = get_db_rows(database.CLIENTS_DB, "SELECT COUNT(*) as cnt FROM invoices")
    inv_num = f"{prefix}-{count_rows[0]['cnt'] + 1:04d}"
    
    inv_id = execute_db_write(
        database.CLIENTS_DB,
        """
        INSERT INTO invoices (invoice_number, client_id, client_name, period, trades_count, gross_pnl, fees, net_pnl, our_fee, net_payout, status, payment_method, issue_date, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (inv_num, invoice.client_id, client_name, invoice.period, invoice.trades_count, invoice.gross_pnl, invoice.fees, invoice.net_pnl, invoice.our_fee, invoice.net_payout, "Pending", invoice.payment_method, issue_date, invoice.notes)
    )
    return {"id": inv_id, "invoice_number": inv_num, "message": "Invoice generated successfully"}

@app.post("/api/invoices/{invoice_id}/pay")
def mark_invoice_paid(invoice_id: int, request: Request):
    paid_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_db_write(
        database.CLIENTS_DB,
        "UPDATE invoices SET status='Paid', paid_date=? WHERE id=?",
        (paid_date, invoice_id)
    )
    return {"message": "Invoice marked as paid"}

# 5. Settings API
@app.post("/api/settings")
def update_settings(settings: SettingsUpdateSchema):
    updates = [
        ("business_name", settings.business_name),
        ("total_bot_lots", str(settings.total_bot_lots)),
        ("default_billing_cycle", settings.default_billing_cycle),
        ("usd_inr_rate", str(settings.usd_inr_rate)),
        ("whatsapp_webhook", settings.whatsapp_webhook),
        ("daily_drawdown_limit", str(settings.daily_drawdown_limit)),
        ("heartbeat_timeout", str(settings.heartbeat_timeout))
    ]
    for key, value in updates:
        execute_db_write(
            database.CLIENTS_DB,
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    return {"message": "Settings updated successfully"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
