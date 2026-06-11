#!/usr/bin/env bash
# Install the secure OpenClaw -> WhatsApp bridge for the polybot watcher.
# Run as root on the VPS:  sudo bash deploy/install_notify_bridge.sh
#
# Effect:
#   * /usr/local/bin/polybot-notify        (root:root 0755) -- the wrapper
#   * /etc/polybot-notify.conf             (root:root 0600) -- recipient (you edit)
#   * /etc/sudoers.d/polybot-notify        (root:root 0440) -- locked sudo rule
# No secret is copied to the bot user. Trading/polybot.service are untouched.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ">> Installing wrapper -> /usr/local/bin/polybot-notify"
install -o root -g root -m 0755 "$SRC/polybot-notify" /usr/local/bin/polybot-notify

if [ ! -f /etc/polybot-notify.conf ]; then
  echo ">> Seeding /etc/polybot-notify.conf (EDIT IT: set POLYBOT_NOTIFY_TO)"
  install -o root -g root -m 0600 "$SRC/polybot-notify.conf.example" /etc/polybot-notify.conf
else
  echo ">> Keeping existing /etc/polybot-notify.conf"
fi

echo ">> Validating + installing sudoers rule"
TMP="$(mktemp)"
cp "$SRC/sudoers.d-polybot-notify" "$TMP"
visudo -cf "$TMP"                                  # fail BEFORE touching /etc
install -o root -g root -m 0440 "$TMP" /etc/sudoers.d/polybot-notify
rm -f "$TMP"
visudo -cf /etc/sudoers.d/polybot-notify           # re-check installed file

cat <<'EOF'

============================================================
 Bridge installed. Next:
   1. Set your number:   sudo nano /etc/polybot-notify.conf   (POLYBOT_NOTIFY_TO)
   2. Test as root:      echo "✅ polybot bridge OK" | /usr/local/bin/polybot-notify
   3. Test as bot user:  echo "✅ via sudo" | sudo -u polymarketbot sudo -n /usr/local/bin/polybot-notify
   4. Point the watcher at it in /opt/polybot/watcher.env:
        WATCHER_SEND_CMD=sudo -n /usr/local/bin/polybot-notify
   5. Restart watcher:   sudo systemctl restart polybot-local-watcher
============================================================
EOF
