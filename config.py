"""
config.py - Shiva Sniper v10  (PINE-ALIGNED 2026-06-03 → TRADE-MATCH-FIX 2026-06-05)

╔═══════════════════════════════════════════════════════════════════════════╗
║ CORRECTION 2026-07-15 — read this before trusting anything below          ║
║                                                                           ║
║ Two entries in the change log below are WRONG and have been reverted:     ║
║   • "PINE_MINTICK 0.1→1.0"  — the correct value is 0.5 (Delta tick size). ║
║   • "t1Off 0.40→0.20"       — Pine's real input is 0.4. Restored.         ║
║                                                                           ║
║ Both were derived by back-solving from live trade geometry instead of     ║
║ reading the TradingView Inputs panel. All 20 trail values have now been   ║
║ transcribed directly from that panel. See the PINE_MINTICK and            ║
║ TRAIL_STAGES blocks below for the full derivation.                        ║
║                                                                           ║
║ RULE: values that exist as inputs in the Pine script are TRANSCRIBED,     ║
║ never solved for. Solving for them is what produced this bug.             ║
╚═══════════════════════════════════════════════════════════════════════════╝

PREVIOUS CHANGES (2026-06-03)
ADX_TREND_TH 17→22, FILTER_ATR_MULT 1.6→1.4, FILTER_BODY_MULT 0.4→0.5,
TREND_RR 5→4, RANGE_RR 3→2.5, TREND_ATR_MULT 0.9→0.6, RANGE_ATR_MULT 0.7→0.5,
MAX_SL_MULT 2→1.5, MAX_SL_POINTS 1500→500, BE_MULT 1→0.6,
TRAIL_OFFSET_FLOOR 0.15→0.0, PINE_MINTICK 0.1→1.0,
BREAKOUT_BUFFER_PTS = 0
TRADE-MATCH FIX (2026-06-05) — Fixes "trade mis + extra trade punch" report
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

# ──────────────────────────────────────
# DELTA EXCHANGE
# ──────────────────────────────────────
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY",    "YOUR_API_KEY")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "YOUR_API_SECRET")
DELTA_TESTNET    = os.environ.get("DELTA_TESTNET", "false").lower() == "true"
SYMBOL    = os.environ.get("SYMBOL",    "BTC/USD:USD")
ALERT_QTY = int(os.environ.get("ALERT_QTY", "1"))
# v10: position size in BTC. Converted to lots via risk.lot_sizing.btc_to_lots
POSITION_BTC_SIZE = float(os.environ.get("POSITION_BTC_SIZE", "0.001"))

# ──────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────
TELEGRAM_ENABLED    = os.environ.get("TELEGRAM_ENABLED", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ──────────────────────────────────────
# WHATSAPP (Meta Business Cloud API)
# ──────────────────────────────────────
WHATSAPP_ACCESS_TOKEN    = os.environ.get("WHATSAPP_ACCESS_TOKEN",     "YOUR_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID",  "YOUR_PHONE_NUMBER_ID")
WHATSAPP_TO_NUMBER       = os.environ.get("WHATSAPP_TO_NUMBER",        "YOUR_TO_NUMBER")
WHATSAPP_VERIFY_TOKEN    = os.environ.get("WHATSAPP_VERIFY_TOKEN",     "YOUR_VERIFY_TOKEN")
WHATSAPP_TEMPLATE_NAME   = os.environ.get("WHATSAPP_TEMPLATE_NAME",    "")
WHATSAPP_TEMPLATE_LANG   = os.environ.get("WHATSAPP_TEMPLATE_LANG",    "en")

# ──────────────────────────────────────
# INDICATOR LENGTHS  (Pine-exact)
# ──────────────────────────────────────
EMA_TREND_LEN = int(os.environ.get("EMA_TREND_LEN", "200"))
EMA_FAST_LEN  = int(os.environ.get("EMA_FAST_LEN",  "50"))
ATR_LEN       = 14
DI_LEN        = 14
ADX_SMOOTH    = 14
ADX_EMA       = 5
RSI_LEN       = 14

# ──────────────────────────────────────
# REGIME THRESHOLDS  (PINE-ALIGNED)
# ──────────────────────────────────────
# Pine: adxTrendTh = 22, adxRangeTh = 18
# Previously 17 to absorb a ~3-point Delta-vs-TV ADX gap. If that gap is
# still real on your data and you miss entries, set ADX_TREND_TH=17 in .env.
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "22"))
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))
# Soft tolerance for ADX comparison. 0.0 = strict Pine match (recommended now
# that ADX_TREND_TH is back to 22). Set higher if you see missed signals.
ADX_TOLERANCE = float(os.environ.get("ADX_TOLERANCE", "0.0"))

# ──────────────────────────────────────
# ENTRY FILTERS  (PINE-ALIGNED)
# ──────────────────────────────────────
# Pine: filterATRMult = 1.4, filterBodyMult = 0.5
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.4"))
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.5"))
# Body filter tolerance (absorbs Delta vs TV OHLC differences).
# 0.0 = strict Pine match. Default 0.05 = lets body of >ATR*0.45 pass.
FILTER_BODY_TOLERANCE = float(os.environ.get("FILTER_BODY_TOLERANCE", "0.0"))
# Volume filter — RE-ENABLED: DEFAULT IS NOW TRUE.
# PREVIOUS BUG: was forced false because Delta REST volumes are ~3% of TV's.
# ROOT CAUSE OF "EXTRA TRADE PUNCHES":
# With BINANCE_SIGNAL_FEED=true (the default), indicator bars come from
# Binance REST + WS — the SAME data source TradingView uses for BTCUSDT.
# Binance volumes are directly comparable to Pine's volSMA, so
# filtersOK = atrOK AND volOK AND bodyOK now matches Pine exactly.
# With the filter OFF, the bot entered on low-volume bars that Pine's
# filtersOK rejected → these appeared as ghost entries vs the Pine list.
# Only set false if BINANCE_SIGNAL_FEED=false (Delta REST data):
# FILTER_VOL_ENABLED=false in .env
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "true").lower() == "true"
FILTER_VOL_MULT    = float(os.environ.get("FILTER_VOL_MULT", "1.0"))

# ─────────────────────────────────────
# RISK / REWARD  (PINE-ALIGNED)
# ──────────────────────────────────────
# Pine: trendRR=4.0, rangeRR=2.5
TREND_RR       = float(os.environ.get("TREND_RR",       "4.0"))
RANGE_RR       = float(os.environ.get("RANGE_RR",       "2.5"))
# Pine: trendATRmul=0.6, rangeATRmul=0.5, maxSLpoints=500
# stopDist = min(atr * atrMult, maxSLPoints)
# With ATR=514:
# Trend SL = min(514 × 0.6, 500) = 308.4 pts
# Range SL = min(514 × 0.5, 500) = 257.0 pts
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.6"))
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.5"))
# Pine: maxSLmul=1.5, maxSLpoints=500
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "1.5"))
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "500.0"))

# ──────────────────────────────────────
# EMERGENCY BRACKET WIDENING  (FIX-BRACKET-INTRABAR)
# ──────────────────────────────────────
# The exchange-side bracket SL is a RESTING stop order on Delta. It fires on
# ANY intrabar touch of last_traded_price. Pine (calc_on_every_tick=false) only
# evaluates the stop at BAR CLOSE. If the bracket sits at the Pine initial SL,
# every intrabar wick that Pine would ignore closes the bot's position early.
# So the bracket must be a CATASTROPHE-ONLY net, placed far beyond the Pine SL.
# Python (TrailMonitor) still owns the real, Pine-exact bar-close SL.
# bracket_dist = clamp(pine_sl_dist * WIDEN_MULT, MIN_PTS, MAX_SL_POINTS)
BRACKET_SL_WIDEN_MULT = float(os.environ.get("BRACKET_SL_WIDEN_MULT", "3.0"))
BRACKET_SL_MIN_PTS    = float(os.environ.get("BRACKET_SL_MIN_PTS",    "300.0"))

# ──────────────────────────────────────
# PINE MINTICK  — CORRECTED 2026-07-15: 1.0 → 0.5
# ──────────────────────────────────────
# The old comment here claimed:
#   "Pine's strategy.exit(trail_points=X, trail_offset=Y) takes X and Y as
#    dimensionless ATR multiples — they are NOT in exchange tick units."
# That claim is FALSE, and it is the root cause of the whole trail divergence.
#
# Pine's strategy.exit(trail_points=, trail_offset=) takes its arguments in
# TICKS (syminfo.mintick units), not price units. Delta India BTCUSD has a
# tick size of 0.5 — visible in every price the exchange prints: they all end
# in .00 or .50 (61,039.50 / 61,092.00 / 62,778.50 / 66,263.50). Never .25.
#
# So the real price distance Pine uses is:
#     offset_in_price = atr × off_mult × 0.5
#
# CONFIRMATION — trade #358 (SHORT, Jun 25 2026):
#     ATR = 262.53, best_price = 61,039.50, TV exit actual = 61,092.00
#     TV's real offset = 61,092.00 − 61,039.50 = 52.50
#     Pine input t1Off = 0.4  (read directly from the TradingView Inputs panel)
#     262.53 × 0.4 × 0.5 = 52.51     ← matches TV to 0.01 pts
#
# HOW THIS BROKE:
# Only the PRODUCT (off_mult × PINE_MINTICK) is observable from a trade.
# Trade #358 pinned that product at 0.20. With PINE_MINTICK wrongly fixed at
# 1.0, the only way to hit 0.20 was to force t1Off from Pine's real 0.4 down
# to 0.20 — which is exactly what BUG-FIX-TRAIL-OFFSET-2026-06-25 did below.
# That made stage 1 land on the correct product BY COINCIDENCE, while leaving
# every other stage — and ALL FIVE activation distances — 2× too wide.
#
# Fixing mintick to 0.5 lets every multiplier below hold its true Pine value.
#
# DO NOT "tune" this. It is a property of the instrument, not a knob.
# Read it off the Pine (syminfo.mintick) or off the exchange's tick size.
PINE_MINTICK = float(os.environ.get("PINE_MINTICK", "0.5"))

# ──────────────────────────────────────────────────────────────────────────────
# DELTA TICK PRICE FIELD   (FIX-TICK-FIELD-MIX  2026-07-15)   ← THE REAL BUG
# ──────────────────────────────────────────────────────────────────────────────
# ROOT CAUSE (trade 2026-07-15 18:30, entry 65146 → exit 65142, TV: 65508.5):
#
#   feed/ws_feed.py did:
#       raw_price = data.get("mark_price") or data.get("last_price") or data.get("close")
#
#   Delta's v2/ticker does NOT carry every field on every update. When
#   mark_price is present the chain yields MARK; when it isn't, the chain
#   silently falls through to CLOSE (last traded). push_delta_tick() therefore
#   received an INTERLEAVED stream of two different prices.
#
# PROOF FROM THE LOG (18:30:14 → 18:30:19, same second, same feed):
#       best_price ratchets : 65203.89 → 65223.44        ← one field
#       breach checks fire  : 65145.90 / 65146.40        ← the other field
#       wick filter caught it: "price 65146.03 jumped -69.97 pts from last=65216.00"
#   A 70-point flap inside one second is not the market. It is two fields.
#
# WHY IT IS FATAL:
#   best_price is a one-way ratchet, so it locks onto the HIGHER cluster.
#   trail_sl = best - offset then lands INSIDE the flap, above the entire
#   lower cluster. The breach then fires on the lower cluster. Once the two
#   clusters sit further apart than trail_offset, the exit is arithmetically
#   guaranteed no matter what the market does.
#
# THE FIX:
#   Pick ONE logical field and never fall back across fields. Use the LAST
#   TRADED price — that is what the TradingView candles (and therefore the
#   bar high the whole trail geometry is derived from) are built out of.
#   mark_price tracks the index, not Delta's book: on the trade above the
#   mark was ~65216 while the exit actually filled at 65142.
#
#   "last" | "last_price" | "close"  → Delta's `close`   (last traded)  ← DEFAULT
#   "mark" | "mark_price"            → Delta's `mark_price`
#   "spot" | "spot_price"            → Delta's `spot_price`
#
# DO NOT set this to "mark" to "reduce noise". Mark is a different instrument.
DELTA_TICK_PRICE_FIELD = os.environ.get("DELTA_TICK_PRICE_FIELD", "last").strip().lower()

# Diagnostic only. Logs (rate-limited) whenever mark_price and close diverge by
# more than this many points. If this fires constantly you have confirmed the
# mixing on your own box. 0 = disable the diagnostic.
DELTA_TICK_DIVERGENCE_WARN_PTS = float(
    os.environ.get("DELTA_TICK_DIVERGENCE_WARN_PTS", "15.0")
)

# ──────────────────────────────────────────────────────────────────────────────
# PINE TICK TRUNCATION   (FIX-TICK-TRUNC  2026-07-15)
# ──────────────────────────────────────────────────────────────────────────────
# Pine's strategy.exit(trail_points=, trail_offset=) takes INTEGER tick counts.
# A fractional tick count is truncated, not rounded.
#     offset_in_price = floor(atr * off_mult) * PINE_MINTICK
#
# VERIFIED against both TV trades on 2026-07-15 — exact to the cent:
#   #400  atr=226.72  floor(226.72*0.4)=90 → 45.0 → 65553.50-45.0 = 65508.50  (TV: 65508.5)
#   #401  atr=240.70  floor(240.70*0.4)=96 → 48.0 → 65501.00-48.0 = 65453.00  (TV: 65453.0)
# (#400 discriminates: round() would give 45.5 → 65508.00 ≠ TV.)
#
# Worth ~0.1-0.4 pts. Free parity. Set false to restore the old float maths.
PINE_TICK_TRUNCATE = os.environ.get("PINE_TICK_TRUNCATE", "false").lower() == "true"

# ──────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (PINE-STAGE-EXACT)
# ──────────────────────────────────────
# Format: (trigger_ATR_mult, trail_points_mult, trail_offset_mult)
# Values verified line-by-line against Pine inputs t1Trig/t1Pts/t1Off … t5*.
#
# ── REVERTED 2026-07-15 — BUG-FIX-TRAIL-OFFSET-2026-06-25 WAS ITSELF THE BUG ──
# That "fix" reverse-engineered t1Off = 0.20 from trade #358's geometry and
# overwrote Pine's real input of 0.4. The arithmetic it used was sound; its
# assumption was not. It solved for off_mult while holding PINE_MINTICK at a
# value (1.0) that was never verified against the Pine source.
#
# Read directly from the TradingView Inputs panel (Shiva Sniper v6.5 — Delta
# India), the true Pine values are:
#     Stage-1  Trigger 0.8   Points 0.5    Offset 0.4
#     Stage-2  Trigger 1.5   Points 0.4    Offset 0.3
#     Stage-3  Trigger 2.5   Points 0.3    Offset 0.25
#     Stage-4  Trigger 4     Points 0.2    Offset 0.15
#     Stage-5  Trigger 6     Points 0.15   Offset 0.1
#
# With PINE_MINTICK correctly set to 0.5, trade #358 now reproduces exactly:
#     262.53 × 0.4 × 0.5 = 52.51 → trail_SL = 61,092.01  (TV: 61,092.00)
# i.e. stage 1 behaves identically to the old (0.20 × 1.0) pairing, while
# stages 2-5 and all five activation distances are no longer 2× too wide.
#
# These values are transcribed from the Pine Inputs panel. They are NOT
# tuning parameters. If a trade does not match, the divergence is elsewhere —
# do not "solve" for these numbers again. That is what caused this bug.
# ─────────────────────────────────────────────────────────────────────────────
TRAIL_STAGES = [
    (0.8,  0.50, 0.40),   # Stage 1   — Pine t1Trig/t1Pts/t1Off  ← RESTORED 0.20→0.40
    (1.5,  0.40, 0.30),   # Stage 2   — Pine t2Trig/t2Pts/t2Off
    (2.5,  0.30, 0.25),   # Stage 3   — Pine t3Trig/t3Pts/t3Off
    (4.0,  0.20, 0.15),   # Stage 4   — Pine t4Trig/t4Pts/t4Off
    (6.0,  0.15, 0.10),   # Stage 5   — Pine t5Trig/t5Pts/t5Off
]

# ──────────────────────────────────────────────────────────────────────────────
# TRAIL ARM TRIGGER  (NOISE FIX 2026-07-18)
# ──────────────────────────────────────────────────────────────────────────────
# PROBLEM (trade 405, 2026-07-17 19:00 SHORT, ATR=212.15):
#   The trail was arming at t1Pts (ATR × 0.50 × mintick = 53.04 pts of profit)
#   but the offset that immediately gets subtracted is t1Off
#   (ATR × 0.40 × mintick = 42.43 pts). That leaves only:
#       53.04 − 42.43 = 10.61 pts of real protection
#   the instant the trail is born. The bot's own feed logs show normal
#   mark/last divergence of 15–40 pts in this exact session. A 10.6-pt stop
#   cannot survive that noise — it will get hit by ordinary jitter, not a
#   real reversal. Trade 405 armed on a small dip, got clipped by a 46-pt
#   bounce, and closed at +7 instead of riding to +320.
#
# FIX: Arm the trail using t1Trig (an ATR multiplier — this is what Pine's
#   trailStage upgrade condition actually uses) instead of t1Pts (a tick
#   count that only defines geometry once a stage is already active):
#       arm distance = ATR × t1Trig = 212.15 × 0.8 = 169.72 pts
#       protection   = 169.72 − 42.43 = 127.29 pts   (12× wider)
#
# On any trade that genuinely runs past both thresholds (the common case),
# this produces the IDENTICAL best_price and exit as before — it only
# changes trades that stall near the old 53-pt threshold and reverse, which
# is exactly the failure mode above. t1Trig/t2Trig/.../t5Trig are already
# real Pine inputs already transcribed into TRAIL_STAGES above; this just
# uses the column that was previously dead for arming purposes.
#
# true  (default) = arm at t1Trig (wide, noise-resistant)
# false            = arm at t1Pts (old behaviour, restores the 10.6pt bug)
TRAIL_ARM_USE_TRIGGER = os.environ.get("TRAIL_ARM_USE_TRIGGER", "true").lower() == "true"

# ──────────────────────────────────────
# TIME-BASED EXIT
# ──────────────────────────────────────
# Pine has NO time exit. Default 0 = full Pine parity.
# If you specifically want "exit at candle close if SL/TP didn't fire",
# set TIME_EXIT_MINUTES=30 (for 30m candles) in your .env. This will FORCE
# the bot to close any open trade 30 min after entry — diverges from Pine
# but matches the same-bar behaviour you may have wanted to enforce.
TIME_EXIT_MINUTES = int(os.environ.get("TIME_EXIT_MINUTES", "0"))

# ──────────────────────────────────────
# BREAKEVEN + RSI  (PINE-ALIGNED)
# ──────────────────────────────────────
# Pine: beMult=0.6
BE_MULT = float(os.environ.get("BE_MULT", "0.6"))
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))
BREAKOUT_BUFFER_PTS = 0
# HISTORY: Was set to 40 to compensate for Delta REST OHLCV being 30–80 pts
# different from TradingView's BTCUSDT candles on the same bar. A bar with
# tv_close barely below tv_prev_low would fire in Pine but NOT in the bot
# (bot's delta_prev_low was lower, so bot didn't see it as a breakout).
# Buffer of 40 was added so bot only fires when the move is unambiguous.
# ROOT CAUSE OF MISSED SIGNALS WITH BINANCE FEED:
# With BINANCE_SIGNAL_FEED=true (the default), prev_high/prev_low come from
# Binance OHLCV — the SAME exchange TradingView uses for BTCUSDT. The Delta
# vs TradingView OHLCV gap no longer exists. A 40pt buffer on identical data
# means the bot misses every Pine trend entry where:
# close > prev_low (Pine fires) but close < prev_low + 40 (bot doesn't).
# Fix: reduce to 5pts (tiny tolerance for REST fetch timing jitter only).
# If you see ghost entries return:  increase to 10 or 15.
# If you see missed signals remain: set to 0 (exact Pine parity with Binance).
# Only set high (30-50) if BINANCE_SIGNAL_FEED=false.
BREAKOUT_BUFFER_PTS = 0

# ──────────────────────────────────────
# COMMISSION + BUFFERS
# ──────────────────────────────────────
COMMISSION_PCT           = 0.05 / 100   # Pine: commission_value=0.05 (percent)
BRACKET_SL_BUFFER        = float(os.environ.get("BRACKET_SL_BUFFER",        "10.0"))
TRAIL_SL_PRE_FIRE_BUFFER = float(os.environ.get("TRAIL_SL_PRE_FIRE_BUFFER", "0.0"))

# ──────────────────────────────────────
# SL CONFIRMATION WINDOW  (FIX-BINANCE-SPIKE)
# ──────────────────────────────────────
# Pine's backtester uses simulated intrabar movement (interpolated OHLC).
# The bot uses real Binance aggTrade ticks (~10ms), which include micro-spikes
# that Pine's model smooths over. A 50-150pt wick lasting <500ms fires the
# bot's Initial SL, while Pine never saw it.
# Fix: require price to stay beyond Initial SL for this many ms before firing.
# Trail SL / TP / Max SL still fire immediately.
# 0 = disabled (instant fire). 1500 = 1.5s (recommended).
SL_CONFIRM_MS = int(os.environ.get("SL_CONFIRM_MS", "1500"))

# ─────────────────────────────────────
# SL CONFIRMATION — CONSECUTIVE DELTA TICK COUNT  (FIX-8 / Option 1+3)
# ──────────────────────────────────────
# When SL_CONFIRM_TICKS > 0, the time-based SL_CONFIRM_MS window is REPLACED
# by a consecutive-tick counter. The bot requires this many consecutive Delta
# Exchange ticks above the SL before firing the exit. Any single Delta tick
# below the SL resets the counter to 0. Binance ticks are completely ignored
# for the breach count — they can never trigger or advance the counter.
# Why tick-count > time-based:
# • Immune to Binance/Delta feed interleaving (the main early-exit cause).
# • Stable across all market conditions — doesn't speed up in fast markets.
# • Simpler to tune: 1 number, no ms estimation needed.
# Recommended starting value: 5
# At ~1 Delta tick/second, 5 ticks ≈ 5 seconds confirmation.
# Cost on a real SL hit: ~5 extra ticks of slippage (typically <10 pts).
# Gain: eliminates premature exits from the Binance/Delta fight.
# Set to 0 to disable and fall back to SL_CONFIRM_MS time-based mode.
SL_CONFIRM_TICKS = int(os.environ.get("SL_CONFIRM_TICKS", "5"))

# ──────────────────────────────────────────────────────────────────
# TRAIL SL CONFIRMATION — POST-ARM  (FIX-TRAIL-INTRABAR)
# ──────────────────────────────────────────────────────────────────
# Once the trail has ARMED the SL is already in profit territory.
# Using SL_CONFIRM_TICKS=5 here means the bot waits ~5 seconds before
# firing a trail exit — Pine fires instantly on the first simulated tick.
# This causes the bot to miss fast intrabar trail exits.
# Use a LOWER threshold post-arm so the trail fires quickly like Pine,
# while the initial SL still gets full spike protection (SL_CONFIRM_TICKS=5).
# 2 = 2 consecutive Delta ticks (~2s)  ← recommended
# 1 = first Delta tick below trail SL  ← closest to Pine, riskier
# 0 = use SL_CONFIRM_TICKS (no separate post-arm threshold)
TRAIL_SL_CONFIRM_TICKS = int(os.environ.get("TRAIL_SL_CONFIRM_TICKS", "2"))

# ──────────────────────────────────────
# TRAIL OFFSET FLOOR  (TUNED FOR BETTER CAPTURE)
# ─────────────────────────────────────
# IMPORTANT: Pine's strategy.exit() trail_points/trail_offset have NO floor.
# However, a small floor prevents the trail from becoming too tight and 
# getting stopped out by normal noise. Reduced from 0.40 to 0.15 to capture 
# more profit while still protecting against extreme wicks.
TRAIL_OFFSET_FLOOR_MULT = float(os.environ.get("TRAIL_OFFSET_FLOOR_MULT", "0.15"))

# ──────────────────────────────────────
# TP HARD EXIT  (FIX-TP-PARITY 2026-06-22)
# ──────────────────────────────────────
# Pine's LIVE strategy has NO strategy.exit(limit=tp) — confirmed by trade #353,
# where TV ran straight through its plotted TP level (65,168) and kept trailing
# another ~225pts to 64,949 before exiting via the trail. TP in Pine is plotted/
# informational only (see phase2/tv_signal_exporter.pine entryTP plot) — it is
# NOT wired to a hard market-close in the live strategy.
# The bot was treating TP as an instant market exit on every tick and on bar
# close, cutting trades short every time they reached the TP distance instead
# of letting the trail run further like TV does.
# false (default, THE FIX) = TP is ignored as an exit trigger entirely.
# Only the trail / Initial SL / Max SL / Time exit can close a trade.
# true = old behavior, TP fires a hard market close.
TP_HARD_EXIT = os.environ.get("TP_HARD_EXIT", "false").lower() == "true"
TRAIL_ARM_FLOOR_MULT    = float(os.environ.get("TRAIL_ARM_FLOOR_MULT",    "0.0"))
SL_FIRE_VIA_BRACKET = os.environ.get("SL_FIRE_VIA_BRACKET", "false").lower() == "true"

# ──────────────────────────────────────
# EXIT PRICE SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────
# False (default, THE FIX): exits run only on the Binance aggTrade feed.
TRAIL_EXIT_FROM_DELTA_WS = os.environ.get("TRAIL_EXIT_FROM_DELTA_WS", "false").lower() == "true"

# ──────────────────────────────────────
# TRAIL SL FIRING SOURCE  (FIX-STALE-CANDLE-HIGH 2026-05-31)
# ──────────────────────────────────────
# False (default, THE FIX): push_ws_candle only advances best_price from the
# FAVOURABLE extreme. Stop fires only via on_price_tick (Binance aggTrade tick).
TRAIL_FIRE_SL_ON_CANDLE_EXTREME = os.environ.get("TRAIL_FIRE_SL_ON_CANDLE_EXTREME", "false").lower() == "true"

# ──────────────────────────────────────
# TIMING
# ──────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")
BINANCE_SIGNAL_FEED = os.environ.get("BINANCE_SIGNAL_FEED", "true").lower() == "true"
BINANCE_SYMBOL      = os.environ.get("BINANCE_SYMBOL", "BTC/USDT")
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "2.0"))  # FIX-10: 2s for position poll
WS_RECONNECT_SEC = 5

# ──────────────────────────────────────
# LOGGING
# ──────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")

# ──────────────────────────────────────
# SLIPPAGE TRACKING (NEW FIX)
# ─────────────────────────────────────
# Alert threshold for slippage as a percentage of ATR.
MAX_EXIT_SLIPPAGE_ATR_PCT = float(os.environ.get("MAX_EXIT_SLIPPAGE_ATR_PCT", "25.0"))

# ─────────────────────────────────────
# PARITY ALIASES  (flat constants for verification — do not use in logic)
# Derived from TRAIL_STAGES list above. Values are identical.
# ──────────────────────────────────────
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
