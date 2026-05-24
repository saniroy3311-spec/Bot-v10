"""
config.py - Shiva Sniper v10

CHANGES IN THIS VERSION:
  FIX-PINE-MINTICK v10.1 | PINE_MINTICK removed from trail activation and offset.
    Pine passes raw USD points to strategy.exit() — no mintick scaling needed.
    activation = atr × pts_mult   (e.g. 310 × 0.70 = 217 pts)
    offset     = atr × off_mult   (e.g. 310 × 0.55 = 170 pts)

  PINE-STAGE-EXACT | Stage upgrade triggers use raw ATR multiples (no PINE_MINTICK).
    Previously: profit_dist >= live_atr × trigger × PINE_MINTICK  (10× too early)
    Now:        profit_dist >= live_atr × trigger                  (correct)

  SL matches Pine exactly:
    Trend: stopDist = min(ATR × 0.9, 433)  → ~281 pts at ATR=312
    Range: stopDist = min(ATR × 0.7, 433)  → ~219 pts at ATR=312
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

# v10: position size in BTC. Converted to lots via risk.lot_sizing.btc_to_lots
#   0.001 BTC =   1 lot   (minimum)
#   0.05  BTC =  50 lots
#   0.1   BTC = 100 lots
#   1.0   BTC = 1000 lots
# Set to 0 to fall back to ALERT_QTY (legacy behaviour).
POSITION_BTC_SIZE = float(os.environ.get("POSITION_BTC_SIZE", "0.001"))

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ──────────────────────────────────────────────
# WHATSAPP (Meta Business Cloud API)
# ──────────────────────────────────────────────
WHATSAPP_ACCESS_TOKEN    = os.environ.get("WHATSAPP_ACCESS_TOKEN",    "YOUR_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "YOUR_PHONE_NUMBER_ID")
WHATSAPP_TO_NUMBER       = os.environ.get("WHATSAPP_TO_NUMBER",       "YOUR_TO_NUMBER")
WHATSAPP_VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN",    "YOUR_VERIFY_TOKEN")

# Template name approved in Meta WhatsApp Manager.
# Using a template bypasses the 24-hour session window so alerts always arrive.
# Set WHATSAPP_TEMPLATE_NAME=bot_alert in your .env once the template is approved.
# Leave blank ("") to fall back to free-form text (subject to 24-h window).
WHATSAPP_TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "")
WHATSAPP_TEMPLATE_LANG = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")

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

# FIX-FEED-DIVERGENCE: ADX_TOLERANCE absorbs the systematic ADX gap between
# Delta REST OHLCV (bot) and TradingView OHLCV (Pine).  Even a 0.5-point
# difference in one historical bar ripples through the 14-period RMA chain
# and can shift the final EMA(5)-smoothed ADX by 0.1–0.5 pts, causing the
# bot to see adx=19.8 while Pine crosses 20.0 and fires.
#
# With ADX_TOLERANCE=0.5:
#   trend_regime fires when adx_smoothed > 19.5  (instead of 20.0)
#   range_regime fires when adx_smoothed < 18.5  (instead of 18.0)
#
# Keep this ≤ 1.0 so regime classification stays meaningful.
# Set ADX_TOLERANCE=0 in .env to restore strict Pine-exact behaviour.
ADX_TOLERANCE = float(os.environ.get("ADX_TOLERANCE", "0.5"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.6"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.4"))

# FIX-FEED-DIVERGENCE: FILTER_BODY_TOLERANCE relaxes the body filter to absorb
# Delta vs TradingView OHLC differences.  Pine computes body size from TV OHLC;
# the bot uses Delta REST OHLC.  A bar where TV sees body > ATR*0.4 may appear
# as body ≈ ATR*0.38 on Delta data — causing filters=FAIL when Pine fires.
#
# The effective threshold becomes:  body > ATR * (FILTER_BODY_MULT - FILTER_BODY_TOLERANCE)
# Default 0.05 means body only needs to be > ATR*0.35 instead of ATR*0.40.
# Set FILTER_BODY_TOLERANCE=0 in .env to restore strict Pine-exact behaviour.
FILTER_BODY_TOLERANCE = float(os.environ.get("FILTER_BODY_TOLERANCE", "0.05"))

FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "false").lower() == "true"
FILTER_VOL_MULT    = float(os.environ.get("FILTER_VOL_MULT", "0.05"))

# ──────────────────────────────────────────────
# RISK / REWARD  (Pine-exact)
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "5.0"))
RANGE_RR       = float(os.environ.get("RANGE_RR",       "3.0"))

# Pine Script:
#   atrMultActive = isTrend ? trendATRmul : rangeATRmul
#   stopDist      = math.min(atr * atrMultActive, maxSLPoints)
# With ATR=312.18:
#   Trend SL = min(312.18 × 0.9, 1500) = 280.96 pts ≈ 281 pts
#   Range SL = min(312.18 × 0.7, 1500) = 218.53 pts
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.9"))
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.7"))

MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "2.0"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "1500.0"))

# ──────────────────────────────────────────────
# PINE MINTICK (FIX-MINTICK-01)
# ──────────────────────────────────────────────
# Applied ONLY to trail_points and trail_offset distances.
# NOT applied to stage upgrade triggers (Pine uses raw ATR multiples there).
# For BTCUSD.P on Delta India: mintick = 0.1
PINE_MINTICK = float(os.environ.get("PINE_MINTICK", "1.0"))  # FIX-BUG4: was 0.1 — fresh deploy would set trail activation to 13pts instead of 131pts
# ^^^ CONFIRMED CORRECT: mintick=0.1 for BTCUSD.P on Delta/TradingView.
# Pine passes atr*trailXPts to strategy.exit(trail_points=...) in TICK units.
# Bot must multiply activation and offset by PINE_MINTICK to get price points.
# Math proof from trade 382: ATR=254.58, peak=57pts, mintick=0.1
#   activation = 254.58 * 0.70 * 0.1 = 17.82 pts  (trail arms at ~18 pts profit)
#   offset     = 254.58 * 0.55 * 0.1 = 14.00 pts  (SL 14 pts behind peak)
#   exit profit = 57 - 14 = 43 pts → exit price = 76785 - 43 = 76742.0 ✓

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (PINE-STAGE-EXACT)
# ──────────────────────────────────────────────
# Format: (trigger_ATR_mult, trail_points_mult, trail_offset_mult)
#
# trigger: stage upgrades when profit_dist >= ATR × trigger  (raw, no mintick)
# pts:     trail arms when peak_profit >= ATR × pts  (raw USD points, no PINE_MINTICK)
# off:     trail SL placed ATR × off behind peak     (raw USD points, no PINE_MINTICK)
#
# With ATR=312.18:
#   Stage 1 upgrades at 312 pts | arms at 21.9 pts | offset 17.2 pts
TRAIL_STAGES = [
    # Confirmed from TradingView inputs panel (screenshots 2026-05-18):
    # trigger: stage upgrades when profit_dist >= ATR × trigger
    # pts:     trail arms when peak_profit >= ATR × pts
    # off:     trail SL placed ATR × off behind peak
    (1.0,  0.70, 0.55),   # Stage 1
    (2.0,  0.55, 0.45),   # Stage 2
    (3.0,  0.45, 0.35),   # Stage 3
    (5.0,  0.30, 0.25),   # Stage 4
    (8.0,  0.20, 0.15),   # Stage 5
]

# ──────────────────────────────────────────────
# TIME-BASED EXIT
# ──────────────────────────────────────────────
# Close any open trade after this many minutes regardless of SL/TP/trail.
# FIX-TIME-EXIT: Pine has NO time exit — default 0 = full Pine parity.
# Override: TIME_EXIT_MINUTES=28 in your .env if you want the time cap.
TIME_EXIT_MINUTES = int(os.environ.get("TIME_EXIT_MINUTES", "0"))

# ──────────────────────────────────────────────
# BREAKEVEN + RSI
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "1.0"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

BREAKOUT_BUFFER_PTS = float(os.environ.get("BREAKOUT_BUFFER_PTS", "30"))

# ──────────────────────────────────────────────
# COMMISSION + BUFFERS
# ──────────────────────────────────────────────
COMMISSION_PCT           = 0.059 / 100
BRACKET_SL_BUFFER        = float(os.environ.get("BRACKET_SL_BUFFER",        "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

SL_FIRE_VIA_BRACKET = os.environ.get("SL_FIRE_VIA_BRACKET", "false").lower() == "true"

# ──────────────────────────────────────────────
# TIMING
# ──────────────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")

BINANCE_SIGNAL_FEED = os.environ.get("BINANCE_SIGNAL_FEED", "true").lower() == "true"
BINANCE_SYMBOL      = os.environ.get("BINANCE_SYMBOL", "BTC/USDT")

TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "5.0"))
WS_RECONNECT_SEC = 5

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")
