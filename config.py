"""
config.py - Shiva Sniper v6.5 Python Bot
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

# ──────────────────────────────────────────────
# DELTA EXCHANGE
# ──────────────────────────────────────────────
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY",    "YOUR_API_KEY")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "YOUR_API_SECRET")
DELTA_TESTNET = os.environ.get("DELTA_TESTNET", "false").lower() == "true"

SYMBOL    = os.environ.get("SYMBOL",    "BTC/USD:USD")
ALERT_QTY = int(os.environ.get("ALERT_QTY", "1"))

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ──────────────────────────────────────────────
# INDICATOR LENGTHS
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
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "22"))   # Pine: adxTrendTh = 22
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.4"))   # Pine: filterATRmul = 1.4
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.5"))   # Pine: filterBody = 0.5
# FIX-VOL-001: Default ON — matches Pine Script filters exactly.
# Set FILTER_VOL_ENABLED=false in .env ONLY if Delta REST returns zero
# volume for all bars and you want to disable the filter entirely.
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "true").lower() == "true"

# ──────────────────────────────────────────────
# RISK / REWARD
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "4.0"))   # Pine: trendRR = 4.0
RANGE_RR       = float(os.environ.get("RANGE_RR",       "2.5"))   # Pine: rangeRR = 2.5
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.6"))   # Pine: trendATRmul = 0.6
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.5"))   # Pine: rangeATRmul = 0.5
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "2.0"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "1500.0"))

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE
# ──────────────────────────────────────────────
TRAIL_STAGES = [
    (1.0,  0.70, 0.55),   # Stage 1
    (2.0,  0.55, 0.45),   # Stage 2
    (3.0,  0.45, 0.35),   # Stage 3
    (5.0,  0.30, 0.25),   # Stage 4
    (8.0,  0.20, 0.15),   # Stage 5
]

# ──────────────────────────────────────────────
# BREAKEVEN + RSI
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "1.0"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

COMMISSION_PCT    = 0.05 / 100
BRACKET_SL_BUFFER = float(os.environ.get("BRACKET_SL_BUFFER", "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "2.0"))
WS_RECONNECT_SEC = 5

LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")

