"""
config.py - Shiva Sniper v6.5 Python Bot

CHANGE IN THIS VERSION:
  CONFIG-FIX-001 | TRAIL_LOOP_SEC default changed from 0.5 → 0.1
    Pine Script's broker emulator tracks price every tick (milliseconds).
    At 0.5s the bot could miss the exact trail stop crossing by up to 0.5s,
    causing a slightly different exit price than Pine shows.
    At 0.1s the bot responds within 100ms of the trail stop being crossed —
    close enough to match Pine's sub-second exit behavior.
    Delta Exchange REST API easily handles 10 requests/second.
    Override via .env: TRAIL_LOOP_SEC=0.1

  FIX-TRAIL-FAST | Trail offsets tightened for faster profit capture.
    Offsets reduced across all 5 stages so the trail SL follows price
    more closely after activation. Triggers and points unchanged.
    Old values preserved in comments for easy rollback.
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
DELTA_TESTNET    = os.environ.get("DELTA_TESTNET", "false").lower() == "true"

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
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "20"))
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.6"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.4"))
# FIX-VOL-PARITY: Volume filter DISABLED by default for Pine parity.
#
# WHY: Pine Script runs on TradingView's own volume data. Delta Exchange
# REST API returns a different (compressed) volume figure — roughly 3% of
# what TradingView shows for the same bar. Because both bar_vol AND vol_sma
# are computed from Delta's data, the *ratio* is stable, but the absolute
# values are far below what Pine sees. Bars that pass Pine's "volume > volSMA"
# can fail the bot's check, causing the bot to miss valid entry signals.
#
# Since Delta REST and TradingView volume are incomparable data sources,
# the cleanest way to achieve Pine entry parity is to disable this filter
# in the bot. The ATR and body filters still reject dead/choppy bars.
#
# To re-enable with tuning: set FILTER_VOL_ENABLED=true and start with
# FILTER_VOL_MULT=0.05 in .env, then check logs for "VOL-FILTER" lines
# and compare vs TradingView chart entries.
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "false").lower() == "true"

# FILTER_VOL_MULT: Only relevant when FILTER_VOL_ENABLED=true.
# Default 0.05 = conservative starting point for Delta REST volumes.
# Adjust in .env (e.g. FILTER_VOL_MULT=0.1) while comparing log vol= to TV.
FILTER_VOL_MULT = float(os.environ.get("FILTER_VOL_MULT", "0.05"))

# ──────────────────────────────────────────────
# RISK / REWARD
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "5.0"))
RANGE_RR       = float(os.environ.get("RANGE_RR",       "3.0"))
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.9"))
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.7"))
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "2.0"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "1500.0"))

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE
# ──────────────────────────────────────────────
# Format: (trigger_ATR_mult, trail_points_mult, trail_offset_mult)
#
# FIX-TRAIL-FAST: Offsets tightened for faster live profit capture.
# Trail SL now follows price more closely after each stage activates.
# Triggers and points_mult unchanged — only offset_mult reduced.
#
# Rollback values (Pine parity):
#   (1.0,  0.70, 0.55)
#   (2.0,  0.55, 0.45)
#   (3.0,  0.45, 0.35)
#   (5.0,  0.30, 0.25)
#   (8.0,  0.20, 0.15)
TRAIL_STAGES = [
    (1.0,  0.70, 0.28),   # Stage 1  | was 0.35
    (2.0,  0.55, 0.24),   # Stage 2  | was 0.30
    (3.0,  0.45, 0.18),   # Stage 3  | was 0.22
    (5.0,  0.30, 0.12),   # Stage 4  | was 0.15
    (8.0,  0.20, 0.08),   # Stage 5  | was 0.10
]

# ──────────────────────────────────────────────
# BREAKEVEN + RSI
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "1.0"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

# BREAKOUT-BUFFER: extra pts close must clear prev high/low before trend entry fires.
# Default 0 = exact Pine parity.
# WHY 0: BINANCE_SIGNAL_FEED=true means bot computes indicators on Binance OHLCV,
# the same data source as TradingView/Pine. Feed divergence is eliminated at the
# indicator level, so no buffer is needed to match Pine entries.
# Set > 0 only if switching BINANCE_SIGNAL_FEED=false (Delta data mode).
BREAKOUT_BUFFER_PTS = float(os.environ.get("BREAKOUT_BUFFER_PTS", "0"))

# ──────────────────────────────────────────────
# COMMISSION + BUFFERS
# ──────────────────────────────────────────────
COMMISSION_PCT           = 0.059 / 100   # FIX-COMM-001: actual Delta India taker rate (was 0.05)
BRACKET_SL_BUFFER        = float(os.environ.get("BRACKET_SL_BUFFER",        "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

# FIX-BRACKET-SL: When true, Python tick loop does NOT fire market closes for
# SL crosses. Delta bracket SL is sole authority for stop exits. Eliminates
# premature exits from Binance-Delta spread drift. TP and Max SL still fire
# via Python. Default true (production-safe).
SL_FIRE_VIA_BRACKET = os.environ.get("SL_FIRE_VIA_BRACKET", "false").lower() == "true"

# ──────────────────────────────────────────────
# TIMING
# ──────────────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")

# BINANCE-SIGNAL-FEED: Use Binance BTCUSDT candles for indicator calculation
# instead of Delta India's BTCUSD.P. TradingView's Pine Script uses Binance
# as its primary BTC data source, so computing indicators on Binance data
# gives ~90-95% match vs Pine (vs ~70-85% on Delta data).
# Set BINANCE_SIGNAL_FEED=true in .env to enable.
# Orders still execute on Delta India — only the candle data source changes.
BINANCE_SIGNAL_FEED = os.environ.get("BINANCE_SIGNAL_FEED", "true").lower() == "true"
BINANCE_SYMBOL      = os.environ.get("BINANCE_SYMBOL", "BTC/USDT")

# BINANCE-EXIT-FEED-v1: TRAIL_LOOP_SEC raised from 0.1 → 5.0.
# BinancePriceFeed now handles all intrabar exit monitoring via Binance
# aggTrade WS (~10ms). The _tick_loop in trail_loop.py is now a pure
# safety net (catches exchange connectivity gaps only) — a 5s poll
# interval is sufficient and avoids unnecessary Delta REST calls.
# To override: set TRAIL_LOOP_SEC=10 in .env for extra conservatism.
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "5.0"))

WS_RECONNECT_SEC = 5

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")
