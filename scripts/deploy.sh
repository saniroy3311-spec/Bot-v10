#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Shiva Sniper v6.5 VPS deployment script
# Run from LOCAL machine: bash scripts/deploy.sh
#
# What this does:
#   1. Installs Python 3.12 + pip on VPS (Ubuntu 24.04)
#   2. Copies bot files to /app/shiva_sniper_bot
#   3. Installs Python dependencies
#   4. Installs and starts systemd service
#   5. Opens firewall port for dashboard
#
# Usage:
#   bash scripts/deploy.sh                  # deploy to server in .env
#   VPS_IP=187.127.136.139 bash scripts/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

VPS_IP="${VPS_IP:-187.127.136.139}"
VPS_USER="${VPS_USER:-root}"
REMOTE_DIR="/app/shiva_sniper_bot"
SERVICE_NAME="shiva_sniper"
DASHBOARD_PORT="10000"

echo "═══════════════════════════════════════════════════"
echo "  Shiva Sniper v6.5 — VPS Deploy"
echo "  Target: ${VPS_USER}@${VPS_IP}"
echo "═══════════════════════════════════════════════════"

# ── 1. Install system dependencies ───────────────────────────────────────────
echo ""
echo "[1/5] Installing Python 3.12 on VPS..."
ssh "${VPS_USER}@${VPS_IP}" "
  apt-get update -qq &&
  apt-get install -y python3.12 python3.12-venv python3-pip git &&
  python3.12 --version
"

# ── 2. Upload bot files ───────────────────────────────────────────────────────
echo ""
echo "[2/5] Uploading bot files..."
ssh "${VPS_USER}@${VPS_IP}" "mkdir -p ${REMOTE_DIR}"

# Rsync everything except phase data, caches, .git
rsync -avz --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='phase*/data/*.csv' \
  --exclude='journal.db' \
  --exclude='*.log' \
  ./ "${VPS_USER}@${VPS_IP}:${REMOTE_DIR}/"

echo "Files uploaded."

# ── 3. Install Python dependencies ───────────────────────────────────────────
echo ""
echo "[3/5] Installing Python dependencies..."
ssh "${VPS_USER}@${VPS_IP}" "
  cd ${REMOTE_DIR} &&
  pip3 install --break-system-packages -r requirements.txt
"

# ── 4. Setup .env if not present ─────────────────────────────────────────────
echo ""
echo "[4/5] Checking .env..."
ENV_EXISTS=$(ssh "${VPS_USER}@${VPS_IP}" "[ -f ${REMOTE_DIR}/.env ] && echo yes || echo no")
if [ "$ENV_EXISTS" = "no" ]; then
  echo "  .env not found — copying .env.example → .env"
  echo "  ⚠️  Edit ${REMOTE_DIR}/.env on the VPS with your real API keys!"
  ssh "${VPS_USER}@${VPS_IP}" "cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env"
else
  echo "  .env already exists — keeping existing config."
fi

# ── 5. Install systemd service ────────────────────────────────────────────────
echo ""
echo "[5/5] Installing systemd service..."
ssh "${VPS_USER}@${VPS_IP}" "
  cp ${REMOTE_DIR}/systemd/shiva_sniper.service /etc/systemd/system/${SERVICE_NAME}.service &&
  systemctl daemon-reload &&
  systemctl enable ${SERVICE_NAME}
"

# Open firewall port for dashboard
ssh "${VPS_USER}@${VPS_IP}" "
  ufw allow ${DASHBOARD_PORT}/tcp 2>/dev/null || true
  echo 'Firewall rule added for port ${DASHBOARD_PORT}'
"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Deploy complete."
echo ""
echo "  NEXT STEPS:"
echo "  1. Edit your API keys:"
echo "     ssh ${VPS_USER}@${VPS_IP} 'nano ${REMOTE_DIR}/.env'"
echo ""
echo "  2. Start the bot:"
echo "     ssh ${VPS_USER}@${VPS_IP} 'systemctl start ${SERVICE_NAME}'"
echo ""
echo "  3. Check status:"
echo "     ssh ${VPS_USER}@${VPS_IP} 'systemctl status ${SERVICE_NAME}'"
echo ""
echo "  4. Live logs:"
echo "     ssh ${VPS_USER}@${VPS_IP} 'journalctl -u ${SERVICE_NAME} -f'"
echo ""
echo "  5. Dashboard URL:"
echo "     http://${VPS_IP}:${DASHBOARD_PORT}"
echo "═══════════════════════════════════════════════════"
