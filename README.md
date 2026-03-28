# Shiva Sniper v6.5 — Python Trading Bot
**High-performance intraday & swing trading bot for Delta Exchange India.**

Shiva Sniper v6.5 is a production-grade trading bot designed to replicate **Pine Script** strategies with 99.9% accuracy. It features a multi-stage trailing stop engine, real-time indicator synchronization, and a built-in web dashboard for performance monitoring.

## 🚀 Key Features
* **TV Accuracy Engine**: Uses `pandas-ta` to mirror TradingView’s `ta.ema`, `ta.atr`, and `ta.rsi` calculations exactly.
* **5-Stage Trail Ratchet**: Dynamic SL management based on ATR multiples to lock in profits as the trade progresses.
* **Dual Regime Detection**: Automatically switches logic between **Trend** (ADX > 22) and **Range** (ADX < 18) markets.
* **Persistence & Recovery**: Powered by a unified journal system (PostgreSQL/SQLite) that survives bot redeploys and server restarts.
* **Real-time Dashboard**: Aiohttp-powered web interface with live P/L charts, trade history, and bot status.

---

## 🛠 Project Structure
```text
├── main.py              # Production entry point & Dashboard server
├── config.py            # Environment-based configuration
├── indicators/
│   └── engine.py        # Core indicator computation (EMA, ATR, RSI, ADX)
├── strategy/
│   └── signal.py        # Entry signal logic
├── risk/
│   └── calculator.py    # SL/TP, Trailing, and Breakeven math
├── infra/
│   ├── journal.py       # Database management (PostgreSQL/SQLite)
│   └── telegram.py      # Real-time alert system
├── orders/
│   └── manager.py       # Delta Exchange execution via CCXT
├── feed/
│   └── ws_feed.py       # OHLCV polling with bar-close timing fix
└── dashboard.html       # Dark-themed UI for performance tracking
```

---

## ⚙️ Configuration
The bot is configured via environment variables. Create a `.env` file in the root directory:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `DELTA_API_KEY` | Your Delta Exchange API Key | `YOUR_API_KEY` |
| `DELTA_API_SECRET` | Your Delta Exchange Secret | `YOUR_API_SECRET` |
| `DELTA_TESTNET` | Set to `true` for sandbox trading | `false` |
| `SYMBOL` | Trading pair (e.g., BTC/USDT:USDT) | `BTC/USDT:USDT` |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather | `YOUR_TOKEN` |
| `TELEGRAM_CHAT_ID` | Your Chat ID | `YOUR_ID` |
| `DATABASE_URL` | PostgreSQL URI (e.g., Supabase) | *(Uses SQLite if empty)* |

---

## 📦 Installation & Deployment

### Docker Deployment
The project includes a `Dockerfile` for easy containerization:
```bash
docker build -t shiva-sniper .
docker run --env-file .env -p 10000:10000 shiva-sniper
```

### VPS Deployment (Ubuntu 24.04)
1. **Prepare the VPS**: Ensure Python 3.12 is installed.
2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Setup Systemd**: Use the provided `shiva_sniper.service` file to run the bot in the background.
   ```bash
   cp systemd/shiva_sniper.service /etc/systemd/system/
   systemctl enable shiva_sniper && systemctl start shiva_sniper
   ```

---

## 📊 Verification Phases
Before going live, follow the built-in verification suite:
1. **Phase 1 (Indicators)**: Run `python phase1/run_phase1.py` to compare Python values vs TV Export.
2. **Phase 2 (Signals)**: Run `python phase2/run_phase2.py` to verify entry/exit bar matching.
3. **Phase 3 (Orders)**: Run `python phase3/run_phase3.py` to test connectivity.

---

Would you like me to generate the Pine Script `tv_exporter.pine` code to help you with the TradingView side of the verification?
