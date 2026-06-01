"""
One-shot patch for main.py — fixes the stale-bar entry block.

Replaces the broken staleness guard (measures from bar OPEN, fires every bar)
with a corrected version (measures from bar CLOSE, fires only on first bar
after restart, with a 120s grace window).

Run: python3 patch_stale_guard.py
"""
import re, sys, shutil, pathlib

TARGET = pathlib.Path("/root/Bot-v10/main.py")
BACKUP = pathlib.Path("/root/Bot-v10/main.py.prepatch")

OLD = '''        # ── 3. Evaluate entry signals (only when flat) ────────────────────────
        # STALENESS GUARD: block entry on first bar after restart
        # Prevents bot from re-entering on a bar that closed before bot started
        bar_ts_ms = float(df["timestamp"].iloc[-1])
        bar_age_sec = (time.time() * 1000 - bar_ts_ms) / 1000
        candle_period_sec = 30 * 60  # 30m
        if bar_age_sec > candle_period_sec:
            logger.warning(
                f"[ENTRY BLOCKED] Bar is {bar_age_sec:.0f}s old > {candle_period_sec}s "
                f"— stale bar, skipping entry to prevent restart re-entry."
            )
            return
'''

NEW = '''        # ── 3. Evaluate entry signals (only when flat) ────────────────────────
        # STALENESS GUARD (patched 2026-05-12):
        # The bar's "timestamp" column is its OPEN time, so a freshly-closed
        # 30m bar is already 1800s old at close. The old check used the bar's
        # period (1800s) as the threshold, which blocked every single bar.
        # Fix: measure age from bar CLOSE (open + period), allow 120s grace
        # for network/processing jitter, and only run on the FIRST bar after
        # restart — that's the stated intent in the comment.
        candle_period_sec = 30 * 60                  # 30m
        STALE_GRACE_SEC   = 120                      # tolerate 2m of jitter
        if not getattr(self, "_first_bar_seen", False):
            bar_ts_ms    = float(df["timestamp"].iloc[-1])
            bar_close_ms = bar_ts_ms + candle_period_sec * 1000
            bar_age_sec  = (time.time() * 1000 - bar_close_ms) / 1000
            self._first_bar_seen = True
            if bar_age_sec > STALE_GRACE_SEC:
                logger.warning(
                    f"[ENTRY BLOCKED] First bar after restart is "
                    f"{bar_age_sec:.0f}s past close > {STALE_GRACE_SEC}s grace "
                    f"— skipping entry to prevent restart re-entry."
                )
                return
'''

src = TARGET.read_text()
if OLD not in src:
    print("ERROR: original stale-guard block not found verbatim in main.py")
    print("Either it was already patched, or whitespace differs.")
    print("Run: grep -n 'STALENESS GUARD' /root/Bot-v10/main.py")
    sys.exit(1)

shutil.copy2(TARGET, BACKUP)
TARGET.write_text(src.replace(OLD, NEW, 1))
print(f"OK — patched {TARGET}")
print(f"     backup at {BACKUP}")
print("Next: pm2 restart delta-bo && pm2 logs delta-bo --lines 50")
