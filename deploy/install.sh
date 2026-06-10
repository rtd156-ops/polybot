#!/usr/bin/env bash
# polybot installer for Ubuntu/Debian. Idempotent-ish; safe to re-run.
# Installs to /opt/polybot under a dedicated unprivileged user.
#
#   sudo bash deploy/install.sh
#
# It does NOT start live trading. It deploys the code, venv and systemd unit;
# you still configure .env and config.local.yaml, then enable the service.
set -euo pipefail

APP_USER="polymarketbot"
APP_DIR="/opt/polybot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> Ensuring system packages (python3, venv, git)..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

echo ">> Creating dedicated user '${APP_USER}' (no login shell)..."
if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
fi

echo ">> Syncing code to ${APP_DIR}..."
mkdir -p "${APP_DIR}"
# Copy repo content (exclude VCS + local state). rsync keeps it repeatable.
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'data' \
  --exclude '.env' --exclude 'config.local.yaml' \
  "${REPO_DIR}/" "${APP_DIR}/"
mkdir -p "${APP_DIR}/data"

echo ">> Creating virtualenv + installing dependencies..."
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo ">> Seeding config + env templates if missing..."
[ -f "${APP_DIR}/config.local.yaml" ] || cp "${APP_DIR}/config.local.yaml.example" "${APP_DIR}/config.local.yaml"
[ -f "${APP_DIR}/.env" ] || cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"

echo ">> Locking down permissions..."
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 600 "${APP_DIR}/.env"
chmod 640 "${APP_DIR}/config.local.yaml"

echo ">> Installing systemd unit..."
cp "${APP_DIR}/deploy/polybot.service" /etc/systemd/system/polybot.service
systemctl daemon-reload

cat <<EOF

============================================================
 polybot installed to ${APP_DIR}
 NEXT STEPS (nothing is running yet):
   1. Edit secrets:   sudo -u ${APP_USER} nano ${APP_DIR}/.env
   2. Edit host cfg:  sudo -u ${APP_USER} nano ${APP_DIR}/config.local.yaml
                      (keep mode: analysis  or  paper)
   3. Smoke test:     sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python \\
                          ${APP_DIR}/scripts/run_once.py --mode paper
   4. Start service:  sudo systemctl enable --now polybot
   5. Watch logs:     journalctl -u polybot -f
============================================================
EOF
