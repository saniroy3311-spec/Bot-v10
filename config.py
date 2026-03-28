"""
config.py - Shiva Sniper v6.5 Python Bot
Secrets loaded from environment variables (Hostinger VPS / GitHub).
Set in your .env file on the VPS (never commit .env to GitHub).

FIX 1 — DELTA_TESTNET now defaults to FALSE.
         The original defaulted to "true" → live API keys were being sent
         to the TESTNET endpoint, which rejects them with "invalid_api_key".
         This was the PRIMARY cause of all entry failures in the screenshot.
         Set DELTA_TESTNET=true in .env ONLY for sandbox testing.

FIX 2 — CANDLE_TIMEFRAME default is "30m" to match the PineScript chart.
         If Pine fires on a different timeframe, change CANDLE_TIMEFRAME
         in your .env file to match exactly.

FIX 3 — Sentinel fallbacks remain "YOUR_API_KEY" so the bot crashes loudly
         on startup when .env is missing, rather than silently failing later.
"""
import os

# ──────────────────────────────────────────────
# DELTA EXCHANGE
# ──────────────────────────────────────────────
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY",    "YOUR_API_KEY")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "YOUR_API_SECRET")

# FIX 1: default → FALSE (live trading). Set DELTA_TESTNET=true for sandbox.
DELTA_TESTNET    = os.environ.get("DELTA_TESTNET", "false").lower() == "true"

SYMBOL      = os.environ.get("SYMBOL",    "BTC/USDT:USDT")
ALERT_QTY   = int(os.environ.get("ALERT_QTY", "30"))
STRATEGY_ID = os.environ.get("STRATEGY_ID", "")

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
# Set FILTER_VOL_ENABLED=false if Delta returns zero volume via REST
# (unit mismatch) — this permanently locks vol_ok=False → no signals.
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",    "1.4"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT",   "0.5"))
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "true").lower() == "true"

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
# ──────────────────────────────────────────────
TRAIL_STAGES = [
    (0.8,  0.5,  0.4 ),
    (1.5,  0.4,  0.3 ),
    (2.5,  0.3,  0.25),
    (4.0,  0.2,  0.15),
    (6.0,  0.15, 0.1 ),
]

# ──────────────────────────────────────────────
# BREAKEVEN + RSI
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "0.6"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

# ──────────────────────────────────────────────
# COMMISSION
# ──────────────────────────────────────────────
COMMISSION_PCT    = 0.05 / 100
BRACKET_SL_BUFFER = float(os.environ.get("BRACKET_SL_BUFFER", "10.0"))

# ──────────────────────────────────────────────
# BOT TIMING
# FIX 2: CANDLE_TIMEFRAME must match your PineScript chart timeframe.
# ──────────────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "1.0"))
WS_RECONNECT_SEC = 5

# ──────────────────────────────────────────────
# STORAGE
# ──────────────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/app/journal.db")
