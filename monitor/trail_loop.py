//@version=6
// ═══════════════════════════════════════════════════════════════════════════════
// Script  : Shiva Sniper — Bot-v10  (1:1 Python Bot Parity)
// Version : v10-PINE-EXACT (Visuals Cleaned)
// ═══════════════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════════════
// DELTA LIVE ALERT CONFIG
// ═══════════════════════════════════════════════════════════════════════════════
alertSymbol = input.string("BTCUSD.P",                          "Delta Symbol",
     tooltip = "Must match Delta Exchange symbol exactly (e.g. BTCUSD.P)")
alertQty    = input.int(1,                                      "Live Alert Qty (lots)",
     minval = 1,
     tooltip = "ALERT_QTY in config.py — only used inside the JSON alert body")
strategyId  = input.string("4e34d08f83a61dfbfa91ad47717b8ed2", "Delta Webhook strategy_id")

strategy(
     title              = "Shiva Sniper — Bot-v10  (1:1 Pine Parity)",
     overlay            = true,
     pyramiding         = 0,
     initial_capital    = 10000,
     default_qty_type   = strategy.fixed,
     default_qty_value  = 1,
     commission_type    = strategy.commission.percent,
     commission_value   = 0.059, // 0.059% taker fees
     slippage           = 10,
     calc_on_every_tick = false)

// ═══════════════════════════════════════════════════════════════════════════════
// ─── INPUTS  (mirrors config.py defaults exactly) ───────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

emaTrendLen = input.int(200, "EMA Trend Length",   minval=50,  maxval=500)
emaFastLen  = input.int(50,  "EMA Fast Length",    minval=10,  maxval=200)
atrLen      = input.int(14,  "ATR Length",         minval=5,   maxval=50)
diLen       = input.int(14,  "DI Length",          minval=5,   maxval=50)
adxSmooth   = input.int(14,  "ADX Smoothing",      minval=5,   maxval=50)
adxEMALen   = input.int(5,   "ADX EMA Smoothing",  minval=1,   maxval=20)
rsiLen      = input.int(14,  "RSI Length",         minval=5,   maxval=50)

adxTrendTh  = input.int(20,  "ADX Trend Threshold (≥ = trend)", minval=15, maxval=40)
adxRangeTh  = input.int(18,  "ADX Range Threshold (< = range)", minval=10, maxval=30)

filterATRmul = input.float(1.6, "Filter: ATR < SMA(ATR,50) × Mult", minval=1.0, maxval=3.0, step=0.1)
filterBody   = input.float(0.4, "Filter: Body > ATR × Mult",        minval=0.1, maxval=2.0, step=0.1)

rsiOB = input.int(70, "RSI Overbought", minval=55, maxval=95)
rsiOS = input.int(30, "RSI Oversold",   minval=5,  maxval=45)

trendRR = input.float(5.0, "Trend R:R", minval=1.0, maxval=10.0, step=0.1)
rangeRR = input.float(3.0, "Range R:R", minval=1.0, maxval=5.0,  step=0.1)

trendATRmul = input.float(0.9, "Trend ATR × SL Mult", minval=0.3, maxval=2.0, step=0.1)
rangeATRmul = input.float(0.7, "Range ATR × SL Mult", minval=0.3, maxval=2.0, step=0.1)

maxSLmult   = input.float(2.0,    "Dynamic Max SL (ATR ×)", minval=1.0, maxval=5.0, step=0.1)
maxSLPoints = input.float(1500.0, "Hard Max SL (Points)",   minval=50,  maxval=5000, step=50.0)

beMult = input.float(1.0, "Breakeven ATR Mult", minval=0.3, maxval=3.0, step=0.1)

trail1Trigger = input.float(1.0,  "Stage 1 Trigger (ATR ×)", minval=0.3,  maxval=3.0,  step=0.1)
trail1Pts     = input.float(0.70, "Stage 1 trail_points ×",  minval=0.1,  maxval=3.0,  step=0.05)
trail1Off     = input.float(0.55, "Stage 1 trail_offset ×",  minval=0.1,  maxval=3.0,  step=0.05)

trail2Trigger = input.float(2.0,  "Stage 2 Trigger (ATR ×)", minval=0.5,  maxval=5.0,  step=0.1)
trail2Pts     = input.float(0.55, "Stage 2 trail_points ×",  minval=0.1,  maxval=3.0,  step=0.05)
trail2Off     = input.float(0.45, "Stage 2 trail_offset ×",  minval=0.1,  maxval=3.0,  step=0.05)

trail3Trigger = input.float(3.0,  "Stage 3 Trigger (ATR ×)", minval=1.0,  maxval=8.0,  step=0.1)
trail3Pts     = input.float(0.45, "Stage 3 trail_points ×",  minval=0.1,  maxval=3.0,  step=0.05)
trail3Off     = input.float(0.35, "Stage 3 trail_offset ×",  minval=0.1,  maxval=3.0,  step=0.05)

trail4Trigger = input.float(5.0,  "Stage 4 Trigger (ATR ×)", minval=2.0,  maxval=12.0, step=0.1)
trail4Pts     = input.float(0.30, "Stage 4 trail_points ×",  minval=0.1,  maxval=3.0,  step=0.05)
trail4Off     = input.float(0.25, "Stage 4 trail_offset ×",  minval=0.1,  maxval=3.0,  step=0.05)

trail5Trigger = input.float(8.0,  "Stage 5 Trigger (ATR ×)", minval=3.0,  maxval=20.0, step=0.1)
trail5Pts     = input.float(0.20, "Stage 5 trail_points ×",  minval=0.05, maxval=2.0,  step=0.05)
trail5Off     = input.float(0.15, "Stage 5 trail_offset ×",  minval=0.05, maxval=2.0,  step=0.05)

// ═══════════════════════════════════════════════════════════════════════════════
// ─── INDICATORS ──────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
emaTrend = ta.ema(close, emaTrendLen)
emaFast  = ta.ema(close, emaFastLen)
atr      = ta.atr(atrLen)
rsi      = ta.rsi(close, rsiLen)

[dip, dim, adxRaw] = ta.dmi(diLen, adxSmooth)
adx = ta.ema(adxRaw, adxEMALen)

atrSMA = ta.sma(atr, 50)
volSMA = ta.sma(volume, 20)

// ═══════════════════════════════════════════════════════════════════════════════
// ─── REGIME ──────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
trendRegime = adx > adxTrendTh
rangeRegime = adx < adxRangeTh

// ═══════════════════════════════════════════════════════════════════════════════
// ─── FILTERS ─────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
filters = atr < atrSMA * filterATRmul and volume > volSMA and math.abs(close - open) > atr * filterBody

// ═══════════════════════════════════════════════════════════════════════════════
// ─── ENTRY CONDITIONS ────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
trendLong  = trendRegime and emaFast > emaTrend and dip > dim and close > high[1] and filters
trendShort = trendRegime and emaFast < emaTrend and dim > dip and close < low[1]  and filters
rangeLong  = rangeRegime and rsi < rsiOS and filters
rangeShort = rangeRegime and rsi > rsiOB and filters

// ═══════════════════════════════════════════════════════════════════════════════
// ─── DELTA JSON BUILDERS (Single Line to prevent CE10156) ────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
qtyStr = str.tostring(alertQty)

deltaEntry(side) =>
    '{"symbol":"' + alertSymbol + '","side":"' + side + '","qty":' + qtyStr + ',"order_type":"market_order","strategy_id":"' + strategyId + '"}'

deltaClose(side) =>
    '{"symbol":"' + alertSymbol + '","side":"' + side + '","qty":' + qtyStr + ',"order_type":"market_order","reduce_only":true,"strategy_id":"' + strategyId + '"}'

// ═══════════════════════════════════════════════════════════════════════════════
// ─── COMMISSION-ADJUSTED P/L (0% Maker Exit Adjusted) ────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
calcRealPL(entryPx, exitPx, qty, isLong) =>
    rawPL = isLong ? (exitPx - entryPx) * qty : (entryPx - exitPx) * qty
    comm  = (entryPx * qty * 0.00059)   // 0.059% taker on entry, 0% maker on exit
    rawPL - comm

// ═══════════════════════════════════════════════════════════════════════════════
// ─── SHARED BAR-CLOSE STATE ──────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
noPosition = strategy.position_size == 0
newBar     = barstate.isconfirmed

var int entryBar      = na
var int entryAlertBar = na

// ═══════════════════════════════════════════════════════════════════════════════
// ─── ENTRIES + WEBHOOK ALERTS ────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
if newBar and noPosition
    if trendLong
        strategy.entry("Trend Long",  strategy.long)
        alert(deltaEntry("buy"),  alert.freq_once_per_bar_close)
        entryAlertBar := bar_index
    else if trendShort
        strategy.entry("Trend Short", strategy.short)
        alert(deltaEntry("sell"), alert.freq_once_per_bar_close)
        entryAlertBar := bar_index
    else if rangeLong
        strategy.entry("Range Long",  strategy.long)
        alert(deltaEntry("buy"),  alert.freq_once_per_bar_close)
        entryAlertBar := bar_index
    else if rangeShort
        strategy.entry("Range Short", strategy.short)
        alert(deltaEntry("sell"), alert.freq_once_per_bar_close)
        entryAlertBar := bar_index

// ═══════════════════════════════════════════════════════════════════════════════
// ─── TRADE STATE ─────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
var float entryPrice = na
var bool  beDone     = false
var int   trailStage = 0

if strategy.position_size != 0 and strategy.position_size[1] == 0
    entryPrice := strategy.position_avg_price
    beDone     := false
    trailStage := 0
    entryBar   := bar_index

if strategy.position_size == 0
    entryPrice    := na
    beDone        := false
    trailStage    := 0
    entryAlertBar := na

// ═══════════════════════════════════════════════════════════════════════════════
// ─── STOP / TARGET ───────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
isTrend  = strategy.opentrades > 0 and (strategy.opentrades.entry_id(0) == "Trend Long" or strategy.opentrades.entry_id(0) == "Trend Short")

rrActive       = isTrend ? trendRR     : rangeRR
atrMultActive  = isTrend ? trendATRmul : rangeATRmul
stopDist       = math.min(atr * atrMultActive, maxSLPoints)

longSL  = entryPrice - stopDist
longTP  = entryPrice + stopDist * rrActive
shortSL = entryPrice + stopDist
shortTP = entryPrice - stopDist * rrActive

// ═══════════════════════════════════════════════════════════════════════════════
// ─── 5-STAGE TRAIL ENGINE ────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
profitDist = not na(entryPrice) ? (strategy.position_size > 0 ? close - entryPrice : entryPrice - close) : 0.0

if not na(entryPrice) and strategy.position_size != 0
    if trailStage < 5 and profitDist >= atr * trail5Trigger
        trailStage := 5
    else if trailStage < 4 and profitDist >= atr * trail4Trigger
        trailStage := 4
    else if trailStage < 3 and profitDist >= atr * trail3Trigger
        trailStage := 3
    else if trailStage < 2 and profitDist >= atr * trail2Trigger
        trailStage := 2
    else if trailStage < 1 and profitDist >= atr * trail1Trigger
        trailStage := 1

activePts = trailStage == 5 ? atr * trail5Pts : trailStage == 4 ? atr * trail4Pts : trailStage == 3 ? atr * trail3Pts : trailStage == 2 ? atr * trail2Pts : atr * trail1Pts
activeOff = trailStage == 5 ? atr * trail5Off : trailStage == 4 ? atr * trail4Off : trailStage == 3 ? atr * trail3Off : trailStage == 2 ? atr * trail2Off : atr * trail1Off

// ═══════════════════════════════════════════════════════════════════════════════
// ─── PRIMARY EXITS ───────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
strategy.exit("Exit TL", from_entry="Trend Long",  stop=longSL,  limit=longTP,  trail_points=activePts, trail_offset=activeOff)
strategy.exit("Exit TS", from_entry="Trend Short", stop=shortSL, limit=shortTP, trail_points=activePts, trail_offset=activeOff)
strategy.exit("Exit RL", from_entry="Range Long",  stop=longSL,  limit=longTP,  trail_points=activePts, trail_offset=activeOff)
strategy.exit("Exit RS", from_entry="Range Short", stop=shortSL, limit=shortTP, trail_points=activePts, trail_offset=activeOff)

// ═══════════════════════════════════════════════════════════════════════════════
// ─── BREAKEVEN ───────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
beTrigger = atr * beMult

if not beDone and not na(entryPrice)
    if strategy.position_size > 0 and close - entryPrice > beTrigger
        strategy.exit("BE-L",  from_entry="Trend Long",  stop=entryPrice, trail_points=activePts, trail_offset=activeOff)
        strategy.exit("BE-LR", from_entry="Range Long",  stop=entryPrice, trail_points=activePts, trail_offset=activeOff)
        beDone := true
    if strategy.position_size < 0 and entryPrice - close > beTrigger
        strategy.exit("BE-S",  from_entry="Trend Short", stop=entryPrice, trail_points=activePts, trail_offset=activeOff)
        strategy.exit("BE-SR", from_entry="Range Short", stop=entryPrice, trail_points=activePts, trail_offset=activeOff)
        beDone := true

// ═══════════════════════════════════════════════════════════════════════════════
// ─── MAX SL ──────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
maxSLthresh = math.min(atr * maxSLmult, maxSLPoints)
var bool maxSLFired = false

if strategy.position_size != 0 and strategy.position_size[1] == 0
    maxSLFired := false

blockExitMaxSL = (not na(entryBar) and bar_index == entryBar) or (not na(entryAlertBar) and bar_index == entryAlertBar)

if not na(entryPrice) and not blockExitMaxSL
    if strategy.position_size > 0 and close <= entryPrice - maxSLthresh
        strategy.close_all(comment="Max SL Hit")
        if not maxSLFired
            alert(deltaClose("sell"), alert.freq_once_per_bar_close)
            maxSLFired := true
    if strategy.position_size < 0 and close >= entryPrice + maxSLthresh
        strategy.close_all(comment="Max SL Hit")
        if not maxSLFired
            alert(deltaClose("buy"), alert.freq_once_per_bar_close)
            maxSLFired := true

// ═══════════════════════════════════════════════════════════════════════════════
// ─── EXIT ALERT DETECTOR ─────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
var int  prevClosedTrades = 0
var bool exitAlertFired   = false

if strategy.position_size == 0 and strategy.position_size[1] != 0
    exitAlertFired := false

if strategy.position_size != 0 and strategy.position_size[1] == 0
    exitAlertFired := false

blockExit = (not na(entryBar) and bar_index == entryBar) or (not na(entryAlertBar) and bar_index == entryAlertBar)

// FLATTENED IF STATEMENT to prevent CE10156
if strategy.closedtrades > prevClosedTrades and newBar and not maxSLFired and not exitAlertFired and not blockExit
    idx   = strategy.closedtrades - 1
    enid  = strategy.closedtrades.entry_id(idx)
    wasL  = str.contains(enid, "Long")

    entryPx  = strategy.closedtrades.entry_price(idx)
    exitPx   = close
    realPL   = calcRealPL(entryPx, exitPx, alertQty, wasL)

    alert(deltaClose(wasL ? "sell" : "buy"), alert.freq_once_per_bar_close)
    exitAlertFired := true

prevClosedTrades := strategy.closedtrades

// ═══════════════════════════════════════════════════════════════════════════════
// ─── VISUALS ─────────────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
plot(emaTrend, "EMA 200",     color=color.orange,              linewidth=2)
plot(emaFast,  "EMA 50",      color=color.new(color.blue, 20), linewidth=1)

plotshape(trendLong  and newBar and noPosition, "Trend Long ▲",  shape.triangleup,   location.belowbar, color.lime,   size=size.small)
plotshape(trendShort and newBar and noPosition, "Trend Short ▼", shape.triangledown, location.abovebar, color.red,    size=size.small)
plotshape(rangeLong  and newBar and noPosition, "Range Long ▲",  shape.triangleup,   location.belowbar, color.aqua,   size=size.small)
plotshape(rangeShort and newBar and noPosition, "Range Short ▼", shape.triangledown, location.abovebar, color.purple, size=size.small)

bgcolor(trendRegime ? color.new(color.blue,   93) : na, title="Trend Regime BG")
bgcolor(rangeRegime ? color.new(color.yellow, 93) : na, title="Range Regime BG")

// ═══════════════════════════════════════════════════════════════════════════════
// ─── ALERTCONDITION BACKUP ───────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════
alertcondition(trendLong  and newBar and noPosition, "Delta: Trend Long Buy",   '{"symbol":"BTCUSD.P","side":"buy","qty":1,"order_type":"market_order","strategy_id":"4e34d08f83a61dfbfa91ad47717b8ed2"}')
alertcondition(trendShort and newBar and noPosition, "Delta: Trend Short Sell", '{"symbol":"BTCUSD.P","side":"sell","qty":1,"order_type":"market_order","strategy_id":"4e34d08f83a61dfbfa91ad47717b8ed2"}')
alertcondition(rangeLong  and newBar and noPosition, "Delta: Range Long Buy",   '{"symbol":"BTCUSD.P","side":"buy","qty":1,"order_type":"market_order","strategy_id":"4e34d08f83a61dfbfa91ad47717b8ed2"}')
alertcondition(rangeShort and newBar and noPosition, "Delta: Range Short Sell", '{"symbol":"BTCUSD.P","side":"sell","qty":1,"order_type":"market_order","strategy_id":"4e34d08f83a61dfbfa91ad47717b8ed2"}')
