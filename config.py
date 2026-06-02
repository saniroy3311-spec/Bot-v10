"""
config.py - Shiva Sniper v10

CHANGES IN THIS VERSION (FIX-2026-05-26):
  FIX-PINE-MINTICK-RESTORE | PINE_MINTICK default reverted from 1.0 → 0.1.
    The 1.0 default caused trail activation to be 10× too late on every trade.
    Math proof (preserved from trade 382): ATR=254.58, peak=57 pts, mintick=0.1
      activation = 254.58 × 0.70 × 0.1 = 17.82 pts   (trail arms ≈ 18 pts profit)
      offset     = 254.58 × 0.55 × 0.1 = 14.00 pts   (SL 14 pts behind peak)
    With mintick=1.0 the activation would be 178 pts — far beyond a typical
    BTC 30m bar move, so the trail almost never armed.

  FIX-ADX-DIVERGENCE | ADX_TREND_TH lowered 20 → 17, ADX_TOLERANCE 0.5 → 1.0.
    Delta REST OHLCV runs ~2–4 ADX points below TradingView's ADX on the same
    bar. Strict 20-threshold caused Trend Long signals to fire 1–2 bars late
    or be missed entirely (e.g. the missed 22:30 IST entry on 2026-05-25 where
    bot adx=16.7, TV adx≥20). Effective bot threshold is now adx > 16.0.

  PINE_MINTICK is applied ONLY in trail_points and trail_offset calculations
  (see monitor/trail_loop.py). Stage upgrade triggers use raw ATR multiples.

  SL matches Pine exactly:
    Trend: stopDist = min(ATR × 0.9, 1500)  → ~281 pts at ATR=312
    Range: stopDist = min(ATR × 0.7, 1500)  → ~219 pts at ATR=312
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
# FIX-ADX-DIVERGENCE: ADX_TREND_TH lowered from 20 → 17 to absorb the
# systematic ~3-point gap between Delta REST OHLCV ADX and TradingView ADX.
# Combined with ADX_TOLERANCE=1.0, the effective trigger becomes adx > 16.0.
# Restore Pine-exact behaviour by setting ADX_TREND_TH=20 in .env.
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "17"))
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# FIX-FEED-DIVERGENCE: ADX_TOLERANCE absorbs the systematic ADX gap between
# Delta REST OHLCV (bot) and TradingView OHLCV (Pine).  Even a 0.5-point
# difference in one historical bar ripples through the 14-period RMA chain
# and can shift the final EMA(5)-smoothed ADX by 0.1–0.5 pts.  In live use
# we observed gaps of 2–4 pts on volatile bars (e.g. 22:30 IST 2026-05-25
# where bot adx=16.7, TV adx≥20.0).
#
# With ADX_TREND_TH=17 and ADX_TOLERANCE=1.0:
#   trend_regime fires when adx_smoothed > 16.0
#   range_regime fires when adx_smoothed < 19.0
#
# Set ADX_TOLERANCE=0 in .env to restore strict Pine-exact behaviour.
ADX_TOLERANCE = float(os.environ.get("ADX_TOLERANCE", "1.0"))

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
# PINE MINTICK
# ──────────────────────────────────────────────
# Applied ONLY to trail_points and trail_offset distances in monitor/trail_loop.py.
# NOT applied to stage upgrade triggers (Pine uses raw ATR multiples there).
#
# For BTCUSD.P on Delta India: mintick = 0.1
# Pine passes atr × trailXPts to strategy.exit(trail_points=...) in TICK units.
# Bot must multiply activation and offset by PINE_MINTICK to get price points.
#
# Math proof from trade 382: ATR=254.58, peak=57 pts, mintick=0.1
#   activation = 254.58 × 0.70 × 0.1 = 17.82 pts   (trail arms ≈ 18 pts profit)
#   offset     = 254.58 × 0.55 × 0.1 = 14.00 pts   (SL 14 pts behind peak)
#   exit profit = 57 − 14 = 43 pts → exit price = 76785 − 43 = 76742.0 ✓
#
# FIX-PINE-MINTICK-RESTORE (2026-05-26): default reverted from 1.0 → 0.1.
# The 1.0 default caused trail activation to be 10× too late on every trade.
PINE_MINTICK = float(os.environ.get("PINE_MINTICK", "0.1"))

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (PINE-STAGE-EXACT)
# ──────────────────────────────────────────────
# Format: (trigger_ATR_mult, trail_points_mult, trail_offset_mult)
#
# trigger: stage upgrades when profit_dist >= ATR × trigger  (raw, no mintick)
# pts:     trail arms when peak_profit >= ATR × pts × PINE_MINTICK
# off:     trail SL placed ATR × off × PINE_MINTICK behind peak
#
# With ATR=312.18 and PINE_MINTICK=0.1:
#   Stage 1 upgrades at 312 pts | arms at 21.9 pts | offset 17.2 pts
TRAIL_STAGES = [
    # Exact values from Pine script inputs (verified 2026-06-02):
    (0.8,  0.50, 0.40),   # Stage 1
    (1.5,  0.40, 0.30),   # Stage 2
    (2.5,  0.30, 0.25),   # Stage 3
    (4.0,  0.20, 0.15),   # Stage 4
    (6.0,  0.15, 0.10),   # Stage 5
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

# ──────────────────────────────────────────────
# TRAIL OFFSET FLOOR  (FIX-TICK-NOISE-WHIPSAW)
# ──────────────────────────────────────────────
# The trail SL can never sit tighter than this fraction of ATR behind
# best_price. Root cause of the +0.50 pt instant exits: the bot evaluates
# the trail on EVERY Binance tick (Pine evaluates on bar OHLC, ~4 prices/bar).
# With PINE_MINTICK=0.1 the stage-1 offset is atr*0.55*0.1 ≈ 7.86 pts — smaller
# than BTC tick noise, so the first noise-bounce after arming fires the stop.
# This floor keeps the offset above the noise band so the trade can develop.
#   0.30 ≈ 43 pts at ATR=143, ≈ 90 pts at ATR=300.
# Raise to give the move more room (captures more, gives back more at exit);
# lower to trail tighter. Set to 0.0 to disable the floor (old behaviour).
TRAIL_OFFSET_FLOOR_MULT = 0.25
TRAIL_ARM_FLOOR_MULT    = 0.25

SL_FIRE_VIA_BRACKET = os.environ.get("SL_FIRE_VIA_BRACKET", "false").lower() == "true"

# ──────────────────────────────────────────────
# EXIT PRICE SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────────────
# When the Binance aggTrade exit feed was added (see binance_price_feed.py),
# the two intrabar Delta-candle push calls in ws_feed.py were SUPPOSED to be
# removed but never were. Those calls push the 30m candle's CUMULATIVE high/low
# to the trail monitor every ~500ms. For a short, the cumulative high (set near
# the candle OPEN) is re-checked against the trail SL on every update — so the
# instant the trail arms at the candle LOW, a high price from minutes earlier
# fires the stop. Pine walks the bar open→high→low→close in order and never
# re-triggers on that stale high.
#
# CONFIRMED on trade #302 (2026-05-31 13:00): best tracked the low (73848) and
# the exit fired on price=73885 (the candle high) in the same instant — only
# push_ws_candle() passes both a low and a high in one call. Bot booked +22pts;
# Pine booked +61.5 by riding the same candle down to its true low.
#
# False (default, THE FIX): exits run only on the Binance aggTrade feed (the
#   intended Pine-matching source) + the REST mark-price safety net. The stale
#   30m candle high can no longer fire the SL.
# True (old behaviour): re-enables the leftover Delta intrabar push.
TRAIL_EXIT_FROM_DELTA_WS = os.environ.get("TRAIL_EXIT_FROM_DELTA_WS", "false").lower() == "true"

# ──────────────────────────────────────────────
# TRAIL SL FIRING SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────────────
# push_ws_candle() receives a candle's high AND low together. For a short it
# updates best_price from the low (correct) and then runs the high through the
# exit check (wrong): the high is cumulative since the candle/bucket opened, so
# once the trail arms at the low, a high price from earlier fires the stop. This
# affects BOTH the Binance 1m bucket feed and (when enabled) the Delta 30m feed.
#
# False (default, THE FIX): push_ws_candle only advances best_price from the
#   FAVOURABLE extreme (low for a short, high for a long). The trail SL is fired
#   only by on_price_tick() against the live trade price (~10ms Binance aggTrade
#   + 5s Delta REST safety net) — which is how Pine retraces into its stop.
# True (old behaviour): candle extremes may fire the SL directly.
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
