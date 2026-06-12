"""
config.py - Shiva Sniper v10  (PINE-ALIGNED 2026-06-03 → TRADE-MATCH-FIX 2026-06-05)

PREVIOUS CHANGES (2026-06-03)
==============================
  ADX_TREND_TH 17→22, FILTER_ATR_MULT 1.6→1.4, FILTER_BODY_MULT 0.4→0.5,
  TREND_RR 5→4, RANGE_RR 3→2.5, TREND_ATR_MULT 0.9→0.6, RANGE_ATR_MULT 0.7→0.5,
  MAX_SL_MULT 2→1.5, MAX_SL_POINTS 1500→500, BE_MULT 1→0.6,
  TRAIL_OFFSET_FLOOR 0.15→0.0, PINE_MINTICK 0.1→1.0,
  BREAKOUT_BUFFER_PTS = 0

TRADE-MATCH FIX (2026-06-05) — Fixes "trade mis + extra trade punch" report
=============================================================================
Four root causes identified for bot trades not matching the Pine trade list:

  FIX-A | FILTER_VOL_ENABLED  false → true  (CRITICAL — extra trade punches)
    CAUSE:  The previous fix disabled the volume filter because Delta REST
            volumes (~3% of TradingView's) made every bar fail volOK.
            BUT BINANCE_SIGNAL_FEED=true was already active — indicator bars
            come from Binance REST (the same source TradingView uses for
            BTCUSDT). Binance volumes ARE directly comparable to Pine's volSMA.
    EFFECT: With filter OFF, bot entered on low-volume bars where Pine's
            filtersOK = false (volOK failed). Every such bar is an "extra punch"
            that has no match on the Pine chart.
    FIX:    Re-enable FILTER_VOL_ENABLED=true now that Binance data is the source.
            Set FILTER_VOL_ENABLED=false in .env only if BINANCE_SIGNAL_FEED=false.

  FIX-B | BREAKOUT_BUFFER_PTS = 0
    CAUSE:  Buffer of 40 was added to compensate for Delta REST OHLCV being
            30–80 pts different from TradingView's. With BINANCE_SIGNAL_FEED=true,
            prev_high/prev_low already come from Binance (= TradingView data).
    EFFECT: The 40pt buffer over-filtered: any Pine trend entry where
            close > prev_low (Pine fires) but close < prev_low + 40 (bot skips)
            was missed. These appeared as "trade mis" in the comparison.
    FIX:    Reduce to 5pts (covers only REST timing jitter; ~1 pip).
            Set to 0 for exact Pine parity. Only use 30–50 if BINANCE_SIGNAL_FEED=false.

  FIX-C | Intrabar stage upgrades REMOVED from trail_loop._evaluate()  (HIGH)
    CAUSE:  trail_loop.py advanced trail stages on every price tick (intrabar).
            Pine with calc_on_every_tick=false only runs its strategy body at
            bar close, so trailStage only upgrades at bar close.
    EFFECT: Bot reached stage 2/3 on an intrabar spike, immediately tightened
            the trail offset, then trailed out at a worse price than Pine.
            These showed as Trail SL exits at different prices vs Pine chart.
    FIX:    Stage upgrades moved to on_bar_close() only (already present there).
            Intrabar block removed from _evaluate() in trail_loop.py.

  FIX-D | Intrabar breakeven REMOVED from trail_loop._evaluate()  (MEDIUM)
    CAUSE:  Same as FIX-C — breakeven (beDone check) fired intrabar when Pine
            only checks it at bar close.
    EFFECT: BE stop armed mid-bar; any pullback before bar close hit the BE stop
            when Pine's BE stop wasn't yet active.
    FIX:    BE check removed from _evaluate(). Remains in on_bar_close() only.

  FIX-E | self.atr updated from current_atr in on_bar_close()  (MEDIUM)
    CAUSE:  Pine recalculates activePts = atr * tNPts and activeOff = atr * tNOff
            every bar using the LIVE ATR (ta.atr is recomputed each bar).
            Bot froze self.atr at the entry-bar ATR.
    EFFECT: When live ATR shrank, Pine's trail offset shrank (tighter trail) but
            bot's trail stayed wide → bot trailed behind Pine's trail SL.
    FIX:    on_bar_close() now updates self.atr = current_atr each bar.

All changes are .env-overridable.
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

# Volume filter — RE-ENABLED: DEFAULT IS NOW TRUE.
#
# PREVIOUS BUG: was forced false because Delta REST volumes are ~3% of TV's.
# ROOT CAUSE OF "EXTRA TRADE PUNCHES":
#   With BINANCE_SIGNAL_FEED=true (the default), indicator bars come from
#   Binance REST + WS — the SAME data source TradingView uses for BTCUSDT.
#   Binance volumes are directly comparable to Pine's volSMA, so
#   filtersOK = atrOK AND volOK AND bodyOK now matches Pine exactly.
#   With the filter OFF, the bot entered on low-volume bars that Pine's
#   filtersOK rejected → these appeared as ghost entries vs the Pine list.
#
# Only set false if BINANCE_SIGNAL_FEED=false (Delta REST data):
#   FILTER_VOL_ENABLED=false in .env
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

# BREAKOUT_BUFFER_PTS = 0
#
# HISTORY: Was set to 40 to compensate for Delta REST OHLCV being 30–80 pts
# different from TradingView's BTCUSDT candles on the same bar. A bar with
# tv_close barely below tv_prev_low would fire in Pine but NOT in the bot
# (bot's delta_prev_low was lower, so bot didn't see it as a breakout).
# Buffer of 40 was added so bot only fires when the move is unambiguous.
#
# ROOT CAUSE OF MISSED SIGNALS WITH BINANCE FEED:
# With BINANCE_SIGNAL_FEED=true (the default), prev_high/prev_low come from
# Binance OHLCV — the SAME exchange TradingView uses for BTCUSDT. The Delta
# vs TradingView OHLCV gap no longer exists. A 40pt buffer on identical data
# means the bot misses every Pine trend entry where:
#   close > prev_low (Pine fires) but close < prev_low + 40 (bot doesn't).
#
# Fix: reduce to 5pts (tiny tolerance for REST fetch timing jitter only).
# If you see ghost entries return:  increase to 10 or 15.
# If you see missed signals remain: set to 0 (exact Pine parity with Binance).
# Only set high (30-50) if BINANCE_SIGNAL_FEED=false.
BREAKOUT_BUFFER_PTS = 0

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
# SL CONFIRMATION — CONSECUTIVE DELTA TICK COUNT  (FIX-8 / Option 1+3)
# ──────────────────────────────────────────────
# When SL_CONFIRM_TICKS > 0, the time-based SL_CONFIRM_MS window is REPLACED
# by a consecutive-tick counter. The bot requires this many consecutive Delta
# Exchange ticks above the SL before firing the exit. Any single Delta tick
# below the SL resets the counter to 0. Binance ticks are completely ignored
# for the breach count — they can never trigger or advance the counter.
#
# Why tick-count > time-based:
#   • Immune to Binance/Delta feed interleaving (the main early-exit cause).
#   • Stable across all market conditions — doesn't speed up in fast markets.
#   • Simpler to tune: 1 number, no ms estimation needed.
#
# Recommended starting value: 5
#   At ~1 Delta tick/second, 5 ticks ≈ 5 seconds confirmation.
#   Cost on a real SL hit: ~5 extra ticks of slippage (typically <10 pts).
#   Gain: eliminates premature exits from the Binance/Delta fight.
#
# Set to 0 to disable and fall back to SL_CONFIRM_MS time-based mode.
SL_CONFIRM_TICKS = int(os.environ.get("SL_CONFIRM_TICKS", "5"))

# ──────────────────────────────────────────────
# TRAIL OFFSET FLOOR  (REMOVED — Pine has no floor)
# ──────────────────────────────────────────────
# IMPORTANT: Pine's strategy.exit() trail_points/trail_offset have NO floor.
# FIX-TRAIL-OFFSET: Pine effective trail offset at stage 0 is ~0.83xATR
# (Jun 8 trade: Pine exited best_price+290pts, ATR=347, off=0.83).
# Stage 1 off_mult=0.40 gives only 139pts — bot exits ~150pts too early.
# Floor raised to 0.40 so offset never narrows below stage 1 level at
# any stage transition. Recovers ~126pts per trade vs previous 0.0 floor.
TRAIL_OFFSET_FLOOR_MULT = float(os.environ.get("TRAIL_OFFSET_FLOOR_MULT", "0.40"))
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
# True  = Pine-exact: Initial SL only fires at bar close (calc_on_every_tick=false)
# False = legacy:     Initial SL fires on every live tick (can exit on intrabar wicks)
# RECOMMENDED: True — this is the single biggest cause of bot-vs-TV divergence.
BAR_CLOSE_SL_EVAL = os.environ.get("BAR_CLOSE_SL_EVAL", "true").lower() == "true"

# Bar-close SL evaluation mode
