"""
config.py - Shiva Sniper v10  (PINE-ALIGNED 2026-06-03)

WHAT CHANGED IN THIS VERSION
============================
Every numeric input in this file was audited against the live Pine script
"Shiva Sniper v6.5 — Delta India". Values that differed were aligned to Pine.
The bot's previous config had drifted from Pine on ~10 parameters, which is
why bot exit prices diverged from Pine chart labels.

Aligned to Pine:
  ADX_TREND_TH        17  → 22       (Pine adxTrendTh)
  FILTER_ATR_MULT     1.6 → 1.4      (Pine filterATRMult)
  FILTER_BODY_MULT    0.4 → 0.5      (Pine filterBodyMult)
  TREND_RR            5.0 → 4.0      (Pine trendRR)
  RANGE_RR            3.0 → 2.5      (Pine rangeRR)
  TREND_ATR_MULT      0.9 → 0.6      (Pine trendATRmul)
  RANGE_ATR_MULT      0.7 → 0.5      (Pine rangeATRmul)
  MAX_SL_MULT         2.0 → 1.5      (Pine maxSLmul)
  MAX_SL_POINTS       1500 → 500     (Pine maxSLpoints)
  BE_MULT             1.0 → 0.6      (Pine beMult)
  TRAIL_OFFSET_FLOOR  0.15 → 0.0     (Pine has NO floor)
  TRAIL_ARM_FLOOR     0.25 → 0.0     (Pine has NO floor)

If you intentionally want any of the OLD values back (for example if the
Delta-vs-TradingView ADX gap is hurting you), override that single key in
your .env — none of the changes above are hard-coded; every value reads
from os.environ first.
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
WHATSAPP_TEMPLATE_NAME   = os.environ.get("WHATSAPP_TEMPLATE_NAME", "")
WHATSAPP_TEMPLATE_LANG   = os.environ.get("WHATSAPP_TEMPLATE_LANG", "en")

# ──────────────────────────────────────────────
# INDICATOR LENGTHS  (Pine-exact)
# ──────────────────────────────────────────────
EMA_TREND_LEN = int(os.environ.get("EMA_TREND_LEN", "200"))
EMA_FAST_LEN  = int(os.environ.get("EMA_FAST_LEN",  "50"))
ATR_LEN       = 14
DI_LEN        = 14
ADX_SMOOTH    = 14
ADX_EMA       = 5
RSI_LEN       = 14

# ──────────────────────────────────────────────
# REGIME THRESHOLDS  (PINE-ALIGNED)
# ──────────────────────────────────────────────
# Pine: adxTrendTh = 22, adxRangeTh = 18
# Previously 17 to absorb a ~3-point Delta-vs-TV ADX gap. If that gap is
# still real on your data and you miss entries, set ADX_TREND_TH=17 in .env.
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "22"))
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# Soft tolerance for ADX comparison. 0.0 = strict Pine match (recommended now
# that ADX_TREND_TH is back to 22). Set higher if you see missed signals.
ADX_TOLERANCE = float(os.environ.get("ADX_TOLERANCE", "0.0"))

# ──────────────────────────────────────────────
# ENTRY FILTERS  (PINE-ALIGNED)
# ──────────────────────────────────────────────
# Pine: filterATRMult = 1.4, filterBodyMult = 0.5
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.4"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.5"))

# Body filter tolerance (absorbs Delta vs TV OHLC differences).
# 0.0 = strict Pine match. Default 0.05 = lets body of >ATR*0.45 pass.
FILTER_BODY_TOLERANCE = float(os.environ.get("FILTER_BODY_TOLERANCE", "0.0"))

# Volume filter — Pine REQUIRES volume > volSMA (volOK)
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "true").lower() == "true"
FILTER_VOL_MULT    = float(os.environ.get("FILTER_VOL_MULT", "1.0"))

# ──────────────────────────────────────────────
# RISK / REWARD  (PINE-ALIGNED)
# ──────────────────────────────────────────────
# Pine: trendRR=4.0, rangeRR=2.5
TREND_RR       = float(os.environ.get("TREND_RR",       "4.0"))
RANGE_RR       = float(os.environ.get("RANGE_RR",       "2.5"))

# Pine: trendATRmul=0.6, rangeATRmul=0.5, maxSLpoints=500
#   stopDist = min(atr * atrMult, maxSLPoints)
# With ATR=514:
#   Trend SL = min(514 × 0.6, 500) = 308.4 pts
#   Range SL = min(514 × 0.5, 500) = 257.0 pts
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.6"))
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.5"))

# Pine: maxSLmul=1.5, maxSLpoints=500
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "1.5"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "500.0"))

# ──────────────────────────────────────────────
# PINE MINTICK
# ──────────────────────────────────────────────
# Pine passes atr × tNPts to strategy.exit(trail_points=...) in TICK units.
# For BTCUSD.P on Delta India: mintick = 0.1
#   activation_in_price = atr × t1Pts × PINE_MINTICK
#   offset_in_price     = atr × t1Off × PINE_MINTICK
PINE_MINTICK = float(os.environ.get("PINE_MINTICK", "0.1"))

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (PINE-STAGE-EXACT)
# ──────────────────────────────────────────────
# Format: (trigger_ATR_mult, trail_points_mult, trail_offset_mult)
# Values verified line-by-line against Pine inputs t1Trig/t1Pts/t1Off … t5*.
TRAIL_STAGES = [
    (0.8,  0.50, 0.40),   # Stage 1   — Pine t1Trig/t1Pts/t1Off
    (1.5,  0.40, 0.30),   # Stage 2   — Pine t2Trig/t2Pts/t2Off
    (2.5,  0.30, 0.25),   # Stage 3   — Pine t3Trig/t3Pts/t3Off
    (4.0,  0.20, 0.15),   # Stage 4   — Pine t4Trig/t4Pts/t4Off
    (6.0,  0.15, 0.10),   # Stage 5   — Pine t5Trig/t5Pts/t5Off
]

# ──────────────────────────────────────────────
# TIME-BASED EXIT
# ──────────────────────────────────────────────
# Pine has NO time exit. Default 0 = full Pine parity.
# If you specifically want "exit at candle close if SL/TP didn't fire",
# set TIME_EXIT_MINUTES=30 (for 30m candles) in your .env. This will FORCE
# the bot to close any open trade 30 min after entry — diverges from Pine
# but matches the same-bar behaviour you may have wanted to enforce.
TIME_EXIT_MINUTES = int(os.environ.get("TIME_EXIT_MINUTES", "0"))

# ──────────────────────────────────────────────
# BREAKEVEN + RSI  (PINE-ALIGNED)
# ──────────────────────────────────────────────
# Pine: beMult=0.6
BE_MULT = float(os.environ.get("BE_MULT", "0.6"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

BREAKOUT_BUFFER_PTS = float(os.environ.get("BREAKOUT_BUFFER_PTS", "0"))

# ──────────────────────────────────────────────
# COMMISSION + BUFFERS
# ──────────────────────────────────────────────
COMMISSION_PCT           = 0.05 / 100   # Pine: commission_value=0.05 (percent)
BRACKET_SL_BUFFER        = float(os.environ.get("BRACKET_SL_BUFFER",        "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

# ──────────────────────────────────────────────
# TRAIL OFFSET FLOOR  (REMOVED — Pine has no floor)
# ──────────────────────────────────────────────
# IMPORTANT: Pine's strategy.exit() trail_points/trail_offset have NO floor.
# Earlier versions of this bot added a floor (0.15) to suppress tick-noise
# whipsaws, but it made bot's offset ~77 pts vs Pine's ~20 pts at ATR=500.
# That was the single biggest divergence between bot and Pine exit prices.
#
# Defaults are now 0.0 (no floor) → exact Pine parity.
# If you see tick-noise whipsaws return, set TRAIL_OFFSET_FLOOR_MULT=0.15
# in your .env to bring back the old protective floor. You'll trade exit
# parity for noise rejection.
TRAIL_OFFSET_FLOOR_MULT = float(os.environ.get("TRAIL_OFFSET_FLOOR_MULT", "0.0"))
TRAIL_ARM_FLOOR_MULT    = float(os.environ.get("TRAIL_ARM_FLOOR_MULT",    "0.0"))

SL_FIRE_VIA_BRACKET = os.environ.get("SL_FIRE_VIA_BRACKET", "false").lower() == "true"

# ──────────────────────────────────────────────
# EXIT PRICE SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────────────
# False (default, THE FIX): exits run only on the Binance aggTrade feed.
TRAIL_EXIT_FROM_DELTA_WS = os.environ.get("TRAIL_EXIT_FROM_DELTA_WS", "false").lower() == "true"

# ──────────────────────────────────────────────
# TRAIL SL FIRING SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────────────
# False (default, THE FIX): push_ws_candle only advances best_price from the
# FAVOURABLE extreme. Stop fires only via on_price_tick (Binance aggTrade tick).
TRAIL_FIRE_SL_ON_CANDLE_EXTREME = os.environ.get("TRAIL_FIRE_SL_ON_CANDLE_EXTREME", "false").lower() == "true"

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
