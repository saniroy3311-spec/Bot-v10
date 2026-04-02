"""
config.py - Shiva Sniper v6.5 Python Bot

CHANGES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
FIX-CFG-1 | SYMBOL default changed "BTC/USDT:USDT" → "BTC/USD:USD"
             Matches your .env SYMBOL=BTC/USD:USD.
             Wrong default caused startup crash when .env env var absent.

FIX-CFG-2 | CANDLE_TIMEFRAME default changed "30m" → "5m"
             Matches your .env CANDLE_TIMEFRAME=5m and Pine chart timeframe.
             30m default caused 6× timing mismatch vs Pine signals.

FIX-CFG-3 | ALERT_QTY default changed "30" → "1"
             Matches your .env ALERT_QTY=1.

FIX-CFG-4 | FILTER_VOL_ENABLED default changed "true" → "false"
             Matches your .env FILTER_VOL_ENABLED=false.
             Delta Exchange REST frequently returns zero volume, which
             permanently blocks all signals when vol filter is enabled.

FIX-CFG-5 | LOG_FILE default path changed to /root/Bot-v10/journal.db
             Matches your actual VPS path from .env.
──────────────────────────────────────────────────────────────────────────────
"""
import os

# ──────────────────────────────────────────────
# DELTA EXCHANGE
# ──────────────────────────────────────────────
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY",    "YOUR_API_KEY")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "YOUR_API_SECRET")

# Default → live trading. Set DELTA_TESTNET=true in .env for sandbox.
DELTA_TESTNET = os.environ.get("DELTA_TESTNET", "false").lower() == "true"

# FIX-CFG-1: Correct default symbol (BTC/USD:USD USD-margined perpetual)
SYMBOL    = os.environ.get("SYMBOL",    "BTC/USD:USD")
# FIX-CFG-3: Default qty = 1 lot (matches your .env)
ALERT_QTY = int(os.environ.get("ALERT_QTY", "1"))

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ──────────────────────────────────────────────
# INDICATOR LENGTHS  (must match PineScript inputs exactly)
# ──────────────────────────────────────────────
EMA_TREND_LEN = int(os.environ.get("EMA_TREND_LEN", "200"))
EMA_FAST_LEN  = int(os.environ.get("EMA_FAST_LEN",  "50"))
ATR_LEN       = 14
DI_LEN        = 14
ADX_SMOOTH    = 14
ADX_EMA       = 5
RSI_LEN       = 14

# ──────────────────────────────────────────────
# REGIME THRESHOLDS
# ──────────────────────────────────────────────
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "22"))
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# FIX-CFG-4: FILTER_VOL_ENABLED defaults to false — Delta REST returns 0
#             volume frequently, which permanently blocks all signals.
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",    "1.4"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT",   "0.5"))
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "false").lower() == "true"

# ──────────────────────────────────────────────
# RISK / REWARD
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "4.0"))
RANGE_RR       = float(os.environ.get("RANGE_RR",       "2.5"))
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.6"))
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.5"))
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "1.5"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "500.0"))

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (trigger_mult, points_mult, offset_mult)
# Exact values from Pine Script v6.5 inputs
# ──────────────────────────────────────────────
TRAIL_STAGES = [
    (0.8,  0.5,  0.4 ),   # Stage 1
    (1.5,  0.4,  0.3 ),   # Stage 2
    (2.5,  0.3,  0.25),   # Stage 3
    (4.0,  0.2,  0.15),   # Stage 4
    (6.0,  0.15, 0.1 ),   # Stage 5
]

# ──────────────────────────────────────────────
# BREAKEVEN + RSI
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "0.6"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

# ──────────────────────────────────────────────
# COMMISSION  (Delta India taker fee 0.05% per side)
# ──────────────────────────────────────────────
COMMISSION_PCT    = 0.05 / 100
BRACKET_SL_BUFFER = float(os.environ.get("BRACKET_SL_BUFFER", "10.0"))

# ──────────────────────────────────────────────
# BOT TIMING
# FIX-CFG-2: CANDLE_TIMEFRAME default = "5m" to match Pine chart + .env.
# ──────────────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "5m")
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "1.0"))
WS_RECONNECT_SEC = 5

# ──────────────────────────────────────────────
# STORAGE
# FIX-CFG-5: Default path matches VPS layout
# ──────────────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")
