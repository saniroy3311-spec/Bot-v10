#!/bin/bash
# status.sh — Check bot health on VPS
# Run on the VPS directly: bash scripts/status.sh

SERVICE="shiva_sniper"
PORT="${PORT:-10000}"
LOG_FILE="${LOG_FILE:-/app/journal.db}"

echo "═══════════════════════════════════════"
echo "  Shiva Sniper v6.5 — Status Check"
echo "═══════════════════════════════════════"

# Service status
echo ""
echo "── SERVICE ──────────────────────────"
systemctl status "$SERVICE" --no-pager -l | head -12

# Last 20 log lines
echo ""
echo "── LAST 20 LOG LINES ────────────────"
journalctl -u "$SERVICE" -n 20 --no-pager

# Dashboard ping
echo ""
echo "── DASHBOARD PING ───────────────────"
if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
  echo "  ✅ Dashboard reachable at http://localhost:${PORT}"
else
  echo "  ❌ Dashboard NOT reachable on port ${PORT}"
fi

# Journal DB
echo ""
echo "── JOURNAL DB ───────────────────────"
if [ -f "$LOG_FILE" ]; then
  SIZE=$(du -sh "$LOG_FILE" | cut -f1)
  echo "  ✅ journal.db exists (${SIZE})"
  if command -v sqlite3 &>/dev/null; then
    TRADES=$(sqlite3 "$LOG_FILE" "SELECT COUNT(*) FROM trades;" 2>/dev/null || echo "N/A")
    OPEN=$(sqlite3 "$LOG_FILE" "SELECT COUNT(*) FROM open_trades;" 2>/dev/null || echo "N/A")
    echo "  Completed trades : ${TRADES}"
    echo "  Open position    : ${OPEN}"
  fi
else
  echo "  ⚠️  journal.db not found at ${LOG_FILE}"
fi

echo ""
echo "═══════════════════════════════════════"
