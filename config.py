"""
config.py - Shiva Sniper v6.5 Python Bot

CHANGES IN THIS VERSION:
──────────────────────────────────────────────────────────────────────────────
BUG-1 FIX | load_dotenv() added — API key / Telegram token now load from .env.
             (Without this every order failed with invalid_api_key.)

30M-OPT   | All strategy parameters tuned for 30-minute timeframe.
             30m candles have ~6× larger ATR than 5m, cleaner trends, and
             bigger directional moves — the following values reflect that:

             CANDLE_TIMEFRAME  : "5m"   → "30m"
             MAX_SL_POINTS     : 500    → 1500   (30m BTC swings ~1000+ pts)
             MAX_SL_MULT       : 1.5    → 2.0    (ATR-based hard cap wider)
             TREND_ATR_MULT    : 0.6    → 0.9    (wider initial SL, less noise)
             RANGE_ATR_MULT    : 0.5    → 0.7
             TREND_RR          : 4.0    → 5.0    (longer trend runs on 30m)
             RANGE_RR          : 2.5    → 3.0
             FILTER_ATR_MULT   : 1.4    → 1.6    (allow larger-ATR candles through)
             FILTER_BODY_MULT  : 0.5    → 0.4    (slightly looser body requirement)
             ADX_TREND_TH      : 22     → 20     (30m trends easier to confirm)
             BE_MULT           : 0.6    → 1.0    (let it breathe before breakeven)
             TRAIL_STAGES      : tighter triggers, larger trail pts for 30m runs
──────────────────────────────────────────────────────────────────────────────
"""
import os

# ── BUG-1 FIX: Load .env FIRST so all os.environ.get() calls below work.
# Without this, DELTA_API_KEY stays "YOUR_API_KEY" → every order fails with
# AuthenticationError: invalid_api_key. Same for TELEGRAM_BOT_TOKEN → 404.
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass   # rely on system env if dotenv not installed

# ──────────────────────────────────────────────
# DELTA EXCHANGE
# ──────────────────────────────────────────────
DELTA_API_KEY    = os.environ.get("DELTA_API_KEY",    "YOUR_API_KEY")
DELTA_API_SECRET = os.environ.get("DELTA_API_SECRET", "YOUR_API_SECRET")

# Default → live trading. Set DELTA_TESTNET=true in .env for sandbox.
DELTA_TESTNET = os.environ.get("DELTA_TESTNET", "false").lower() == "true"

SYMBOL    = os.environ.get("SYMBOL",    "BTC/USD:USD")
ALERT_QTY = int(os.environ.get("ALERT_QTY", "1"))

# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID")

# ──────────────────────────────────────────────
# INDICATOR LENGTHS  (unchanged — standard across timeframes)
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
# 30M-OPT: ADX_TREND_TH lowered 22 → 20.
#   On 30m, a reading of 20 already indicates a well-established trend.
#   At 22, too many valid 30m trend setups were filtered out.
# ──────────────────────────────────────────────
ADX_TREND_TH = int(os.environ.get("ADX_TREND_TH", "20"))   # was 22
ADX_RANGE_TH = int(os.environ.get("ADX_RANGE_TH", "18"))

# ──────────────────────────────────────────────
# ENTRY FILTERS
# 30M-OPT: FILTER_ATR_MULT raised 1.4 → 1.6.
#   30m candles naturally have a larger ATR relative to its SMA baseline.
#   The original 1.4 cap was rejecting valid breakout bars on 30m.
# 30M-OPT: FILTER_BODY_MULT lowered 0.5 → 0.4.
#   30m bodies include more noise-cancellation; a slightly smaller body
#   still represents a meaningful directional move.
# FILTER_VOL_ENABLED stays false — Delta REST frequently returns 0 volume.
# ──────────────────────────────────────────────
FILTER_ATR_MULT    = float(os.environ.get("FILTER_ATR_MULT",  "1.6"))   # was 1.4
FILTER_BODY_MULT   = float(os.environ.get("FILTER_BODY_MULT", "0.4"))   # was 0.5
FILTER_VOL_ENABLED = os.environ.get("FILTER_VOL_ENABLED", "false").lower() == "true"

# ──────────────────────────────────────────────
# RISK / REWARD
# 30M-OPT:
#   TREND_ATR_MULT  0.6 → 0.9  — 30m bars are noisier within each candle;
#                                  a 0.6 ATR stop gets hit by normal wicks.
#   RANGE_ATR_MULT  0.5 → 0.7  — same reason for range trades.
#   TREND_RR        4.0 → 5.0  — 30m trends carry further; higher RR justified.
#   RANGE_RR        2.5 → 3.0  — range reversals also larger on 30m.
#   MAX_SL_MULT     1.5 → 2.0  — ATR-based hard cap scaled for 30m volatility.
#   MAX_SL_POINTS   500 → 1500 — BTC 30m can gap 1000+ pts; 500 was too tight.
# ──────────────────────────────────────────────
TREND_RR       = float(os.environ.get("TREND_RR",       "5.0"))    # was 4.0
RANGE_RR       = float(os.environ.get("RANGE_RR",       "3.0"))    # was 2.5
TREND_ATR_MULT = float(os.environ.get("TREND_ATR_MULT", "0.9"))    # was 0.6
RANGE_ATR_MULT = float(os.environ.get("RANGE_ATR_MULT", "0.7"))    # was 0.5
MAX_SL_MULT    = float(os.environ.get("MAX_SL_MULT",    "2.0"))    # was 1.5
MAX_SL_POINTS  = float(os.environ.get("MAX_SL_POINTS",  "1500.0")) # was 500.0

# ──────────────────────────────────────────────
# 5-STAGE TRAIL ENGINE  (trigger_mult, points_mult, offset_mult)
# 30M-OPT: Triggers are slightly earlier (price moves are bigger on 30m,
#   so we can ratchet the trail faster without early-exiting).
#   Trail points are wider to avoid premature exit on 30m retracements.
#
#   Stage  | 5m was              | 30m now
#   -------+---------------------+---------------------
#   1      | (0.80, 0.50, 0.40) | (1.00, 0.70, 0.55)
#   2      | (1.50, 0.40, 0.30) | (2.00, 0.55, 0.45)
#   3      | (2.50, 0.30, 0.25) | (3.00, 0.45, 0.35)
#   4      | (4.00, 0.20, 0.15) | (5.00, 0.30, 0.25)
#   5      | (6.00, 0.15, 0.10) | (8.00, 0.20, 0.15)
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
# 30M-OPT: BE_MULT raised 0.6 → 1.0.
#   On 30m, trades need more room to breathe before locking in breakeven.
#   At 0.6 ATR, BE was triggering too early and getting stopped out at 0
#   on normal 30m pullbacks that would have continued in the right direction.
# ──────────────────────────────────────────────
BE_MULT = float(os.environ.get("BE_MULT", "1.0"))  # was 0.6
RSI_OB  = int(os.environ.get("RSI_OB", "70"))
RSI_OS  = int(os.environ.get("RSI_OS", "30"))

# ──────────────────────────────────────────────
# COMMISSION  (Delta India taker fee 0.05% per side)
# ──────────────────────────────────────────────
COMMISSION_PCT    = 0.05 / 100
BRACKET_SL_BUFFER = float(os.environ.get("BRACKET_SL_BUFFER", "10.0"))

# ──────────────────────────────────────────────
# BOT TIMING
# 30M-OPT: CANDLE_TIMEFRAME changed "5m" → "30m".
#   The WS feed subscribes to candlestick_30m automatically.
#   TRAIL_LOOP_SEC kept at 2.0 — slightly relaxed from 1.0 since 30m
#   candles move slower tick-by-tick; saves unnecessary REST calls.
# ──────────────────────────────────────────────
CANDLE_TIMEFRAME = os.environ.get("CANDLE_TIMEFRAME", "30m")   # was "5m"
TRAIL_LOOP_SEC   = float(os.environ.get("TRAIL_LOOP_SEC", "2.0"))  # was 1.0
WS_RECONNECT_SEC = 5

# ──────────────────────────────────────────────
# STORAGE
# ──────────────────────────────────────────────
LOG_FILE = os.environ.get("LOG_FILE", "/root/Bot-v10/journal.db")
