# polybot — Deploy & Operations (Ubuntu/Debian VPS, 24/7)

This guide brings polybot up on a VPS running **analysis** or **paper** mode
under systemd. Live trading stays **OFF** and requires a separate, deliberate
step documented at the end.

> Golden rules
> - Secrets live only in `.env` (`chmod 600`), never in git, never in logs.
> - The service runs as an unprivileged `polymarketbot` user.
> - State persists in SQLite under `data/` so restarts are clean.
> - `mode: live` does nothing unless `execution.live_enabled: true` **and**
>   CLOB credentials are present — otherwise the bot refuses to start.

---

## 1. Install dependencies & deploy

Clone the repo onto the VPS (e.g. into `/root/polybot`), then run the installer.
It creates the user, copies code to `/opt/polybot`, builds a venv, installs the
systemd unit, and locks down permissions.

```bash
git clone <your-fork-url> polybot
cd polybot
git checkout claude/polymarket-bot-v1-3oqb63
sudo bash deploy/install.sh
```

Manual equivalent (if you prefer not to use the script):

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git rsync
sudo useradd --system --create-home --shell /usr/sbin/nologin polymarketbot
sudo mkdir -p /opt/polybot && sudo rsync -a --exclude .git --exclude .venv ./ /opt/polybot/
cd /opt/polybot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
```

## 2. Create a dedicated user

Done by the installer. To do it by hand:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin polymarketbot
sudo chown -R polymarketbot:polymarketbot /opt/polybot
```

## 3. Configure `.env` (secrets) and `config.local.yaml` (host settings)

```bash
sudo -u polymarketbot cp /opt/polybot/.env.example /opt/polybot/.env
sudo -u polymarketbot cp /opt/polybot/config.local.yaml.example /opt/polybot/config.local.yaml

sudo -u polymarketbot nano /opt/polybot/.env             # WEBHOOK_URL etc.
sudo -u polymarketbot nano /opt/polybot/config.local.yaml # keep mode: paper

# Lock secrets down:
sudo chmod 600 /opt/polybot/.env
sudo chown polymarketbot:polymarketbot /opt/polybot/.env
```

For analysis/paper you only need (optionally) `WEBHOOK_URL` / `WEBHOOK_TOKEN`.
Leave all `POLY_*` keys blank.

## 4. Test with `run_once`

```bash
# One cycle, paper mode, as the service user:
sudo -u polymarketbot /opt/polybot/.venv/bin/python \
    /opt/polybot/scripts/run_once.py --mode paper

# Just scan markets (read-only):
sudo -u polymarketbot /opt/polybot/.venv/bin/python \
    /opt/polybot/scripts/scan_markets.py --show-rejected
```

## 5. Install & start the systemd service

```bash
sudo cp /opt/polybot/deploy/polybot.service /etc/systemd/system/polybot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polybot
```

The unit runs `run_once.py --loop` (continuous), restarts on failure
(`Restart=on-failure`, 10s backoff), and is capped at 512M RAM / 50% CPU — tune
these in the unit file for your box.

## 6. View logs (journalctl)

```bash
journalctl -u polybot -f                 # live tail
journalctl -u polybot --since "1 hour ago"
journalctl -u polybot -p warning         # warnings+errors only
```

Logs are structured JSON (one object per line). The redaction filter guarantees
no secret-shaped value is ever emitted.

## 7. Restart / stop / status

```bash
sudo systemctl status polybot
sudo systemctl restart polybot
sudo systemctl stop polybot
sudo systemctl disable polybot   # stop starting at boot
```

## 8. Run reports

```bash
cd /opt/polybot
sudo -u polymarketbot .venv/bin/python scripts/paper_report.py
sudo -u polymarketbot .venv/bin/python scripts/opportunities_report.py --days 7
```

## 9. Enable / disable the webhook

- Enable: set `WEBHOOK_URL` (and optional `WEBHOOK_TOKEN`) in `.env`, ensure
  `notifications.enabled: true` in `config.local.yaml`, then
  `sudo systemctl restart polybot`.
- Disable: set `notifications.enabled: false` (or blank `WEBHOOK_URL`) and
  restart. Per-event toggles live under `notifications.events`.

---

## 10. Going live (LATER — controlled, deliberate)

Do **not** do this until paper results over a meaningful window look good.

1. On a secured host, install the live extra:
   `/opt/polybot/.venv/bin/pip install -r requirements.txt && .venv/bin/pip install py-clob-client web3`
2. Put CLOB credentials in `.env` (`POLY_PRIVATE_KEY`, `POLY_CLOB_API_KEY`,
   `POLY_CLOB_API_SECRET`, `POLY_CLOB_API_PASSPHRASE`). Keep `chmod 600`.
3. In `config.local.yaml` set **both**:
   ```yaml
   mode: live
   execution:
     live_enabled: true
     live_order_type: limit          # required
     live_max_order_notional_usd: 10 # start tiny
   ```
4. The bot will refuse to start if credentials are missing or the order type
   isn't `limit`. The live execution methods in `polybot/execution/live.py` are
   stubbed (`LiveExecutionDisabled`) in V1 — wire and test them against the CLOB
   sandbox before trusting real funds.
5. Start with the smallest possible size, watch `journalctl` and
   `paper_report`/`opportunities_report`, and scale up only gradually.

### Backups

The whole state is one SQLite file:

```bash
sudo -u polymarketbot sqlite3 /opt/polybot/data/polybot.db ".backup '/opt/polybot/data/backup-$(date +%F).db'"
```
