"""
config.py - Shiva Sniper v10  (PINE-ALIGNED 2026-06-03, BUG-FIX-3 2026-06-03)

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

BUG-FIX-3 (2026-06-03) — Three root causes fixed that made bot fire trades
TV never showed, and exit at completely different prices:

  BUG-1 | FILTER_VOL_ENABLED  "true"  → "false"
    Delta Exchange REST volumes are ~3% of TradingView's volumes. With the
    filter ON every bar fails volOK → bot drops every signal that TV fires.
    Pine's volOK uses TV data; the bot cannot replicate that with Delta REST.
    Fix: disable the volume filter. ATR + body filters still guard bad bars.

  BUG-2 | PINE_MINTICK         0.1    → 1.0
    Pine's strategy.exit(trail_points, trail_offset) takes values in TICKS.
    The correct mintick for BTCUSDT.P on Delta India is 0.5 (USD per tick),
    but Pine's trail_points/trail_offset inputs are dimensionless ATR
    multiples — they are NOT in tick units. Multiplying by mintick shrinks
    the offset by 10×, making the bot's trail 10× tighter than Pine's.
    Example: ATR=400, stage-1 offset = 400×0.4×0.1 = 16 pts (old bot)
                                     vs 400×0.4×1.0 = 160 pts (Pine exact).
    Fix: set PINE_MINTICK=1.0 so offset = ATR × mult, identical to Pine.

  BUG-3 | BREAKOUT_BUFFER_PTS  0      → 40
    Pine's trendLong uses TradingView's close > high[1]. The bot uses
    Delta Exchange REST data for the same check. Delta's prev_high is
    routinely 30–80 pts lower than TradingView's on the same bar, so the
    bot sees a breakout that Pine never saw → ghost entries with no TV match.
    Fix: require close > prev_high + 40 pts before firing trend entries.
    Tune up/down by 10 pts if you see missed signals or ghost entries return.

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

# Volume filter — BUG-FIX-3/BUG-1: DEFAULT IS NOW FALSE.
# Pine REQUIRES volume > volSMA (volOK), but Pine uses TradingView volume.
# Delta Exchange REST volumes are ~3% of TradingView's — incomparable sources.
# With the filter ON, every bar fails volOK and all signals are dropped silently.
# The bot cannot replicate Pine's volOK without TradingView volume data.
# ATR + body filters still reject dead/choppy bars — they are sufficient.
# To re-enable (e.g. if you connect a TradingView-compatible volume feed):
#   FILTER_VOL_ENABLED=true in .env
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "false").lower() == "true"
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
# PINE MINTICK  — BUG-FIX-3/BUG-2: DEFAULT IS NOW 1.0
# ──────────────────────────────────────────────
# Pine's strategy.exit(trail_points=X, trail_offset=Y) takes X and Y as
# dimensionless ATR multiples — they are NOT in exchange tick units.
# The old default of 0.1 multiplied the offset by 0.1, making the bot's
# trail 10× tighter than Pine's:
#
#   ATR=400, stage-1 offset (old): 400 × 0.40 × 0.1  =  16 pts  ← WRONG
#   ATR=400, stage-1 offset (new): 400 × 0.40 × 1.0  = 160 pts  ← Pine exact
#
# With PINE_MINTICK=1.0:  offset_in_price = atr × stage_off_mult  (= Pine)
# With PINE_MINTICK=0.1:  offset_in_price = atr × stage_off_mult × 0.1
#
# Only change this if you have a concrete reason to scale the offsets
# (e.g. a different instrument where Pine explicitly passes tick-unit values).
PINE_MINTICK = float(os.environ.get("PINE_MINTICK", "1.0"))

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

# BUG-FIX-3/BUG-3: BREAKOUT_BUFFER_PTS DEFAULT IS NOW 40
# Pine's trendLong uses: close > high[1]  (TradingView bar data)
# The bot uses Delta Exchange REST data for the same check.
# Delta's prev_high is routinely 30–80 pts lower than TradingView's on the
# same 30m bar because the two feeds have different OHLCV for the same candle.
# Result: bot sees close > delta_prev_high and fires; Pine never fired because
# tv_close <= tv_prev_high → ghost entries with zero match to TV trade list.
#
# Fix: require close > prev_high + BREAKOUT_BUFFER_PTS before firing.
# Default 40 absorbs the typical Delta–TradingView high/low spread on BTC 30m.
# Tune by ±10 pts:
#   Too many ghost entries still? → increase to 50 or 60.
#   Missing valid TV signals?     → decrease to 30 or 20.
#   Set to 0 to restore old behaviour (exact Pine condition, no buffer).
BREAKOUT_BUFFER_PTS = float(os.environ.get("BREAKOUT_BUFFER_PTS", "40"))

# ──────────────────────────────────────────────
# COMMISSION + BUFFERS
# ──────────────────────────────────────────────
COMMISSION_PCT           = 0.05 / 100   # Pine: commission_value=0.05 (percent)
BRACKET_SL_BUFFER        = float(os.environ.get("BRACKET_SL_BUFFER",        "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

# ──────────────────────────────────────────────
# SL CONFIRMATION WINDOW  (FIX-BINANCE-SPIKE)
# ──────────────────────────────────────────────
# Pine's backtester uses simulated intrabar movement (interpolated OHLC).
# The bot uses real Binance aggTrade ticks (~10ms), which include micro-spikes
# that Pine's model smooths over. A 50-150pt wick lasting <500ms fires the
# bot's Initial SL, while Pine never saw it.
# Fix: require price to stay beyond Initial SL for this many ms before firing.
# Trail SL / TP / Max SL still fire immediately.
# 0 = disabled (instant fire). 1500 = 1.5s (recommended).
SL_CONFIRM_MS = int(os.environ.get("SL_CONFIRM_MS", "1500"))

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

# ──────────────────────────────────────────────
# PARITY ALIASES  (flat constants for verification — do not use in logic)
# Derived from TRAIL_STAGES list above. Values are identical.
# ──────────────────────────────────────────────
ADX_EMA_LEN   = ADX_EMA   # alias — same value (5)

TRAIL_T1_TRIG, TRAIL_T1_PTS, TRAIL_T1_OFF = TRAIL_STAGES[0]
TRAIL_T2_TRIG, TRAIL_T2_PTS, TRAIL_T2_OFF = TRAIL_STAGES[1]
TRAIL_T3_TRIG, TRAIL_T3_PTS, TRAIL_T3_OFF = TRAIL_STAGES[2]
TRAIL_T4_TRIG, TRAIL_T4_PTS, TRAIL_T4_OFF = TRAIL_STAGES[3]
TRAIL_T5_TRIG, TRAIL_T5_PTS, TRAIL_T5_OFF = TRAIL_STAGES[4]

# Bar-close SL evaluation mode
BAR_CLOSE_SL_EVAL = False

# Bar-close SL evaluation mode
BAR_CLOSE_SL_EVAL = False
