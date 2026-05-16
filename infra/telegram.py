"""
infra/telegram.py — Shiva Sniper v10  (PATCH: adds Points Captured)
──────────────────────────────────────────────────────────────────────
Only the two changed methods are shown below — drop them into the
existing infra/telegram.py file, replacing the old versions.

Everything else (lifecycle, daily summary, transport) stays identical.
──────────────────────────────────────────────────────────────────────
"""

from risk.lot_sizing import compute_points, lots_to_btc


# ── Entry (now logs qty + BTC face) ──────────────────────────────────────────
async def notify_entry(
    self,
    signal_type : str,
    entry_price : float,
    sl          : float,
    tp          : float,
    atr         : float,
    qty         : int = None,
) -> None:
    is_long = "Long" in signal_type
    emoji   = "🟢" if is_long else "🔴"
    side    = "LONG" if is_long else "SHORT"
    sl_dist = abs(entry_price - sl)
    tp_dist = abs(tp - entry_price)
    rr      = tp_dist / sl_dist if sl_dist > 0 else 0
    qty_str = ""
    if qty:
        qty_str = f"  |  <code>{qty}</code> lots  ({lots_to_btc(qty):.4f} BTC)"
    await self._send(
        f"{emoji} <b>ENTRY — {side}</b>{qty_str}\n"
        f"<code>{Telegram._now_ist()}</code>\n\n"
        f"Fill  : <b>${entry_price:,.2f}</b>\n"
        f"SL    : <code>${sl:,.2f}</code>  (-{sl_dist:.2f})\n"
        f"TP    : <code>${tp:,.2f}</code>  (+{tp_dist:.2f})\n"
        f"ATR   : <code>{atr:.2f}</code>  |  R:R <code>{rr:.2f}</code>"
    )


# ── Exit (now reports Points Captured explicitly) ────────────────────────────
async def notify_exit(
    self,
    reason      : str,
    entry_price : float,
    exit_price  : float,
    real_pl     : float,
    is_long     : bool = True,
    qty         : int  = None,
) -> None:
    emoji   = "💰" if real_pl >= 0 else "🔻"
    pl_sign = "+" if real_pl >= 0 else ""
    side    = "LONG" if is_long else "SHORT"
    points  = compute_points(entry_price, exit_price, is_long)
    pts_sign = "+" if points >= 0 else ""
    qty_str = f"  |  <code>{qty}</code> lots" if qty else ""

    await self._send(
        f"{emoji} <b>EXIT — {side}</b>{qty_str}\n"
        f"<code>{Telegram._now_ist()}</code>\n\n"
        f"Entry  : <code>${entry_price:,.2f}</code>\n"
        f"Exit   : <b>${exit_price:,.2f}</b>\n"
        f"<b>Points Captured</b> : <code>{pts_sign}{points:.2f}</code>\n"
        f"P&amp;L    : <b>{pl_sign}{real_pl:.4f} USD</b>\n"
        f"Reason : <code>{reason}</code>"
    )
