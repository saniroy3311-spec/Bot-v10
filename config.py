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
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "20"))   # Pine v6.5-30M: adxTrendTh = 20 (30M-OPT-004, was 22 on 5m)
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.6"))   # Pine v6.5-30M: filterATRmul = 1.6 (30M-OPT-005, was 1.4 on 5m)
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.4"))   # Pine v6.5-30M: filterBody = 0.4 (30M-OPT-005, was 0.5 on 5m)
# FIX-VOL-001: Default ON — matches Pine Script filters exactly.
# Set FILTER_VOL_ENABLED=false in .env ONLY if Delta REST returns zero
# volume for all bars and you want to disable the filter entirely.
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "true").lower() == "true"

# ──────────────────────────────────────────────
# RISK / REWARD
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "5.0"))   # Pine v6.5-30M: trendRR = 5.0 (30M-OPT-003, was 4.0 on 5m)
RANGE_RR       = float(os.environ.get("RANGE_RR",       "3.0"))   # Pine v6.5-30M: rangeRR = 3.0 (30M-OPT-003, was 2.5 on 5m)
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.9"))   # Pine v6.5-30M: trendATRmul = 0.9 (30M-OPT-002, was 0.6 on 5m)
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.7"))   # Pine v6.5-30M: rangeATRmul = 0.7 (30M-OPT-002, was 0.5 on 5m)
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

