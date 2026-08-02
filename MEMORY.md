# Bot-v10 (Shiva Sniper) — Project Memory

**Last updated:** 2026-07-15
**Purpose:** Stop re-solving problems that are already solved. Read this before changing anything.

---

## 0. The Prime Rule

> **Values that exist as inputs in the Pine script are TRANSCRIBED, never solved for.**

Every major bug in this project's history came from breaking this rule — back-solving a
number from live trade geometry instead of opening the TradingView Inputs panel and
reading it.

Two examples that cost months:

| Bug | Cause | Truth |
|---|---|---|
| `ADX_TREND_TH=23` | Drifted from Pine | Pine says `22`. ADX 22.1 → TV entered, bot didn't. |
| `t1Off=0.20` | Reverse-engineered from trade #358 | Pine says `0.4`. The "fix" broke a correct value. |

If a trade doesn't match, the divergence is somewhere else. **Do not solve for these numbers.**

---

## 1. Verified Pine Inputs (Shiva Sniper v6.5 — Delta India)

Transcribed from the TradingView Inputs panel, 2026-07-15. These are ground truth.

| Pine Input | Value | `.env` / `config.py` key |
|---|---|---|
| EMA Trend Length | 200 | |
| EMA Fast Length | 50 | |
| ATR Length | 14 | |
| DI Length | 14 | |
| ADX Smoothing | 14 | |
| ADX EMA Smooth | 5 | |
| RSI Length | 14 | |
| ADX Trend Threshold | **22** | `ADX_TREND_TH` |
| ADX Range Threshold | 18 | `ADX_RANGE_TH` |
| ATR Filter Mult | 1.4 | `FILTER_ATR_MULT` |
| Body Filter Mult | 0.5 | `FILTER_BODY_MULT` |
| Trend R:R | 4 | `TREND_RR` |
| Range R:R | 2.5 | `RANGE_RR` |
| Trend SL ATR Mult | 0.6 | `TREND_ATR_MULT` |
| Range SL ATR Mult | 0.5 | `RANGE_ATR_MULT` |
| Max SL ATR Mult | 1.5 | `MAX_SL_MULT` |
| Max SL (Points) | 500 | `MAX_SL_POINTS` |
| Breakeven ATR Mult | 0.6 | `BE_MULT` |
| RSI Overbought | 70 | `RSI_OB` |
| RSI Oversold | 30 | `RSI_OS` |

### Trail engine

| Stage | Trigger | Points | Offset |
|---|---|---|---|
| 1 | 0.8 | 0.50 | **0.40** |
| 2 | 1.5 | 0.40 | 0.30 |
| 3 | 2.5 | 0.30 | 0.25 |
| 4 | 4.0 | 0.20 | 0.15 |
| 5 | 6.0 | 0.15 | 0.10 |

Lives in `config.py: TRAIL_STAGES`.

---

## 2. PINE_MINTICK = 0.5 — settled, do not touch

Pine's `strategy.exit(trail_points=, trail_offset=)` takes arguments in **ticks**
(`syminfo.mintick`), not price units. Delta India BTCUSD tick size is **0.5**.

```
offset_in_price = atr × off_mult × 0.5
```

**Two independent proofs:**

1. **Trade #358** (SHORT, 2026-06-25): ATR=262.53, best_price=61,039.50, TV exit=61,092.00.
   `262.53 × 0.4 × 0.5 = 52.51` → trail_SL = **61,092.01**. TV: 61,092.00. Error: **0.01 pts**.

2. **Fill history**: every real Delta fill is a multiple of 0.5 —
   `64,694.0 / 64,762.0 / 64,693.0 / 64,922.0 / 64,922.5`. Never `.25`. That *is* the tick size.

### Why this is not a tuning knob

Only the **product** `off_mult × PINE_MINTICK` is observable from a trade.
`0.4 × 0.5 = 0.20`. Setting `mintick=1.0` forces `off_mult=0.20` to compensate — which is
exactly the bug that existed for a month. **`PINE_MINTICK` and `off_mult` are one knob with
two names.** Changing one without the other does nothing but break the calibration.

---

## 3. TradingView: what you're actually comparing against

### The backtest is a 4-point guess

TradingView's broker emulator sees only OHLC per bar and **assumes** the intrabar path:

- If the **high** is closer to the open → assumes `open → high → low → close`
- If the **low** is closer to the open → assumes `open → low → high → close`

This holds on historical bars **even with `calc_on_every_tick=true`**.

Your bot sees real ticks. TV sees 4 photos and imagines the rest. That is the entire
"97.5 vs 230" gap — not a bug.

### The exit LABEL repaints. The ALERT does not.

Live, the label plots on the real breach tick. Once the bar closes and goes historical,
**TradingView erases and redraws it** using the emulator's fiction.

> **Compare against the ALERT, never the label.**

### Bar Magnifier (untested — worth doing)

`strategy(..., use_bar_magnifier = true)` replaces the guess with lower-timeframe data.
TV's number should fall **toward** the bot's. Requires Premium. **This has not been tried yet.**

### What can never be closed

- Real order slippage: 5–20 pts
- Delta (traded) vs chart feed drift: 10–25 pts

**Realistic target: match the alert within 20–40 points.** Matching the backtest exactly
would require having no stop loss for 30 minutes at a time. Don't.

---

## 4. Measurement reality — read before trusting any data

### The journal fabricates exit prices

`trail_loop.py:879` (ghost-trail-guard):
```python
real_fill = await self._order_mgr.fetch_bracket_fill_price()
bracket_exit_price = real_fill if real_fill is not None else float(self._state.current_sl)
                                                             # ↑ invents a number
```
`fetch_bracket_fill_price()` returns `None` when `_product_symbol` is unset — guaranteed on
**recovered** positions (`place_entry()` never ran). It then writes the trail SL as if it
were a fill.

**How to spot a fake row:** real Delta fills are always multiples of 0.5.
`64636.0228629734` and `62033.4925715897` are arithmetic, not fills.

**Confirmed damage (3 of 40 rows):**

| id | journal says | reality (Delta fills) | error |
|---|---|---|---|
| 83 | +229.00 pts / +$6.87 | +0.5 pts / +$0.02 | **+228.5** |
| 81 | −125.98 pts | −69.0 pts | **−57.0** |

Row 81 was a **normal entry** (`Trend Long`, qty=33), not a recovery — so the fabrication is
**not** recovery-only. Root cause there is still unknown; check logs for
`fetch_bracket_fill_price layer-1 failed`.

### The 5-stage trail engine has never run

40 trades. **All 23 trail exits were `Trail SL (stage 0)`.** Stages 2–5 are dead code.

Stage 1 needs `profit ≥ 0.8 × ATR` (~178 pts at ATR 222). The trail arms at ~55 and sits
44 behind the peak — price has never run to 178 without first retracing 44 from an interim
high. **Any analysis of stages 2–5 is theoretical.**

### The strategy loses money

```
Trail SL (stage 0)   23   +1716.1
Initial SL           14   −2367.4
ghost-trail-guard     3     −36.5
                          ─────────
                          −687.8 pts over 40 trades  (≈ −17/trade)
```

14 Initial SL hits averaging **−169 each** eat everything the trail earns.

> **TV parity will not fix this.** Before spending more effort closing a 30-point gap, check
> whether TV's Strategy Tester shows the same shape over the same 40 trades. If TV is also
> negative → parity works, the strategy is the problem. If TV is positive → the gap is
> somewhere not yet looked at.

---

## 5. Fixed — do not re-break

| Date | Fix | Why |
|---|---|---|
| 2026-07-15 | `PINE_MINTICK` 1.0 → **0.5** | Delta tick size. Activation was 2× too wide (111 vs 55.6) on **every** trade. This was the real TV gap. |
| 2026-07-15 | `t1Off` 0.20 → **0.40** | Restores Pine's real input. Pairs with mintick 0.5 → product 0.20 (unchanged). |
| 2026-07-15 | Recovery direction (`manager.py:229`) | ccxt 4.4.57 `delta.parse_position()` emits `side='buy'/'sell'`, **never** `'long'`. `side == "long"` was always False → **every** recovered position adopted as SHORT. |
| earlier | `ADX_TREND_TH` 23 → **22** | Pine says 22. Cost trade #372. |

### Post-fix expectation (UNTESTED as of 2026-07-15)

| | before | after |
|---|---|---|
| arms at | +111 | **+55.6** |
| trail_off | 44.45 | 44.45 |
| stop sits at | +67 | **+11.2** |

Trail arms earlier and parks the stop ~11 pts above entry. **Tighter → expect more small
scratches, and stage 1 becomes even rarer.** P&L could move either way. Watch the first 5 trades.

---

## 6. Open bugs

| # | Bug | Location | Severity |
|---|---|---|---|
| 1 | **Strategy is −687 pts over 40 trades** | strategy design | **highest** |
| 2 | Ghost-guard fabricates exit prices | `trail_loop.py:879`, `manager.py:438` | high — corrupts all measurement |
| 3 | Recovery offset poisoning | `trail_loop.py` `on_price_tick` | high |
| 4 | `close_position_market()` doesn't exist | called at `telegram_controller.py:148` | medium — AttributeError |
| 5 | Stale docstrings | `trail_loop.py:14` claims bar-close-only stage upgrades; line 1015 does them intrabar | medium — actively misleading |
| 6 | `ADX_TOLERANCE=0.5` has no Pine counterpart | `.env` | unknown — investigate |
| 7 | Delta API keys leaked in a screenshot | — | **rotate** |

### Bug 3 detail — recovery offset poisoning

```
11:30:04  offset locked: binance=64943.99  delta=64922.00  offset=+21.99   ← correct
11:31:45  offset locked: binance=64900.00  delta=64922.00  offset=−22.00   ← wrong
11:33:38  offset locked: binance=64878.01  delta=64922.00  offset=−43.99   ← worse
```
`delta=64922.00` never changes — it's the **stale entry fill**, not a live price. So on
recovery `offset = current_binance − historical_entry` = **your P&L**, not a feed spread.
It poisons every bar price in `on_bar_close()`, and triggers repeated
`Offset recal rejected: jump=59.28 > max=10.0` because `RECAL_MAX_JUMP=10` blocks the
correction.

---

## 7. Architecture facts

- **Feed:** `BINANCE_SIGNAL_FEED=false`. Charts Delta, trades Delta. Pine is titled
  "Delta India". **FIX-6 routing `best_price` to Delta ticks is CORRECT** — do not "fix" it
  to Binance.
- **Endpoint:** `https://api.india.delta.exchange` (set via `urls` override in
  `build_exchange()`). ccxt's default global endpoint returns `invalid_api_key` — the keys
  are region-specific.
- **Contract size:** 1 contract = 0.001 BTC.
- **Pine trail firing:** once armed, `strategy.exit()` fires **intrabar** in live Pine.
  `BAR_CLOSE_SL_EVAL` applies only to the Initial SL (strategy body, bar-close only).
- **Intrabar stage upgrade** (`trail_loop.py:1015`) is **live**, and is a **deliberate
  deviation from Pine** (`calc_on_every_tick=false` → Pine upgrades stages at bar close only).
  Kept for profit capture, not parity. Currently moot — stage 0 is all that ever runs.

### The unresolved decision

**Two goals fight each other:** *match TradingView* vs *capture more points*.

Half the bot chases each. That's why nothing converges. Pick one — it settles the stage
question, the hold guard, FIX-17, and everything downstream automatically.

Recommendation: **match TV first**, because it's the only goal that's measurable. Establish
a true baseline, then deviate deliberately, one change at a time, measuring each.

---

## 8. The 30-knobs problem

Free parameters: `PINE_MINTICK`, `TRAIL_OFFSET_FLOOR_MULT`, 15 numbers in `TRAIL_STAGES`,
`TRAIL_SL_BREACH_HOLD_SECS`, `..._STAGE_UP`, `TRAIL_SL_LARGE_BREACH_ATR_PCT`,
`TRAIL_MEDIAN_WINDOW`, `MAX_DELTA_TICK_JUMP`, `WICK_STREAK_CONFIRM`, `WICK_STALE_TIMEOUT_S`,
`TRAIL_SL_CONFIRM_TICKS`, `TRAIL_SL_PRE_FIRE_BUFFER`, `RECAL_MAX_JUMP`, `SL_CONFIRM_MS`… **30+.**

Verified data points: **one** (trade #358) plus a handful of anecdotes.

You cannot identify 30 parameters from 1 equation. Every miss spawned a new knob, and each
knob absorbs an unknown blend of feed differences, slippage, repainting, and the *previous*
knobs' errors. FIX-13 needed FIX-14 to rescue it. FIX-15 needed FIX-17 to rescue it.

**That is not progress. That is a system fitting its own noise.**

> **Rule: do not add another knob. Add a measurement.**

Note that most of these guards were tuned against a bot whose trail armed 2× late. Their
justification may have evaporated with the mintick fix. **Re-test before trusting them.**

---

## 9. Operations

### `.env` is not in git (correct — it has keys). Consequences:

- Real config is **invisible** to git history. This is exactly where `ADX_TREND_TH=23` hid.
- **`.env` has duplicate keys.** `load_dotenv(override=True)` → **last line wins.** Editing
  the first one silently does nothing. `PINE_MINTICK` had two.

```bash
# find duplicate keys
sort .env | grep -v '^#' | grep '=' | cut -d= -f1 | uniq -d
```

**TODO:** commit a `.env.example` — keys blanked, all other values real.

### Changing a `.env` value (safe pattern — kills duplicates)

```bash
cd /root/Bot-v10
cp .env /root/.env.backup-$(date +%s)      # backups go OUTSIDE the repo
sed -i '/^KEY_NAME=/d' .env
echo "KEY_NAME=value" >> .env
grep -n "KEY_NAME" .env                     # must show exactly ONE line
pm2 restart delta-bot --update-env          # --update-env is REQUIRED
```

### Deploying code

File copying to this VPS has failed repeatedly. **Use in-place heredoc patches:**

```bash
cd /root/Bot-v10 && cp orders/manager.py /root/manager.py.bak-$(date +%s) && python3 << 'PYEOF'
p = 'orders/manager.py'
src = open(p).read()
old = '''...exact text...'''
new = '''...replacement...'''
if 'MARKER' in src:      print('= already patched')
elif old not in src:     print('X PATTERN NOT FOUND - NO CHANGE'); raise SystemExit(1)
else:                    open(p,'w').write(src.replace(old, new, 1)); print('OK patched')
PYEOF
python3 -c "import ast; ast.parse(open('orders/manager.py').read()); print('syntax OK')"
```

Always: back up outside the repo, refuse to patch on pattern mismatch, verify syntax.

> **Note:** `#` comments inside a pasted code block are **ignored by bash**. Instructions
> like `# now edit the file` will be silently skipped. This has caused two failed deploys.

### Verification commands

```bash
# trail parameters live?  → want activation_pts=55.56, trail_off=44.45
pm2 logs delta-bot --lines 50 --nostream | grep "TRAIL] Started"

# clean startup?  → want "Flat on Delta — cancelled all stale bracket orders"
pm2 logs delta-bot --lines 200 --nostream | grep -i "Flat on Delta\|ghost\|Open position"

# exchange truth: position, orphan orders, real fills
cd /root/Bot-v10 && python3 -c "
import asyncio
from orders.manager import build_exchange
from config import SYMBOL
async def m():
    ex = build_exchange()
    pos = [p for p in await ex.fetch_positions([SYMBOL]) if abs(float(p.get('contracts') or 0)) > 0]
    print('POSITIONS:', pos if pos else 'FLAT')
    print('OPEN ORDERS:', [(o.get('id'), o.get('type'), o.get('side'), o.get('price'))
                           for o in await ex.fetch_open_orders(SYMBOL)] or 'NONE')
    for t in await ex.fetch_my_trades(SYMBOL, limit=5):
        print('FILL:', t.get('datetime'), t.get('side'), t.get('amount'), '@', t.get('price'))
    await ex.close()
asyncio.run(m())
"
```

### Journal schema (`journal.db`, gitignored — back it up)

`trades`: `id, ts, signal_type, is_long, entry_price, exit_price, sl, tp, atr, qty,
points_captured, real_pl, exit_reason, trail_stage`

```bash
cp /root/Bot-v10/journal.db /root/journal.db.bak-$(date +%s)

sqlite3 -header -column journal.db \
"SELECT exit_reason, COUNT(*) n, ROUND(SUM(points_captured),1) total_pts
 FROM trades GROUP BY exit_reason ORDER BY n DESC;"
```

### Quantity — two settings that disagree

| setting | used by | |
|---|---|---|
| `ALERT_QTY` | `manager.py:272 amount=ALERT_QTY` | **the order actually sent** |
| `POSITION_BTC_SIZE` | `main.py:118 btc_to_lots()` | **what journal/Telegram report** |

`main.py:495` `[QTY-FIX]` reconciles them from the real fill — but only when there **is** a
fill. On a recovered trade there isn't, so it keeps the pre-computed number. That's why
trade 83 logged `qty=30` while Delta filled 33.

**Keep them equal.** `real_pl` in historical rows was computed with inconsistent sizes;
`points_captured` is unaffected.

---

## 10. Next actions, in order

1. **Watch 5 trades.** Confirm `activation_pts=55.56` each time. Change nothing else.
   The mintick fix altered every trade's behaviour and is completely untested.
2. **Rotate the Delta API keys.**
3. **Bar Magnifier** — one line in the Pine. May shrink the TV gap honestly, for free.
4. **Fix the ghost-guard**: re-hydrate `_product_symbol` / `_is_long` on recovery, and
   **never** fall back to `current_sl`. Log the exit as unknown rather than inventing a number.
   *Nothing downstream is measurable until the journal is trustworthy.*
5. **Then** confront the −687 points.

---

## Appendix — session log

**2026-07-15**
- Audited all 20 Pine inputs against `.env` (the audit that found everything).
- Found `t1Off` was corrupted from 0.4 → 0.20 by a "fix"; `PINE_MINTICK` should be 0.5.
- Fixed recovery direction bug (confirmed by reading ccxt 4.4.57 source, not guessing).
- Discovered the journal fabricates exit prices (3/40 rows).
- Discovered stages 2–5 have never executed.
- Discovered the strategy is −687 pts over 40 trades.
- Commits: `824fea7` (`manager.py`), plus an earlier `config.py` commit.

**Earlier sessions**
- `ADX_TREND_TH` 23 → 22 (cost trade #372).
- FIX-17 large-breach fast exit (`TRAIL_SL_LARGE_BREACH_ATR_PCT=15`, admitted as "a starting
  estimate, not backtested").
- Intrabar stage upgrade re-enabled by request; parity tradeoff left undecided.
- `cancel_bracket` 404 race condition — flagged, never addressed.
