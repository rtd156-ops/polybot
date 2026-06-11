# polybot local watcher (LLM-free outbox alerts)

Replaces the OpenClaw/Codex cron that woke an LLM every minute (burning tokens on
empty cycles) with a tiny local daemon. It tails `data/outbox.jsonl`, alerts only
on real events using fixed templates, and delivers via a command you configure.
**No LLM, no tokens, no secrets in the repo.**

It does **not** touch trading, strategies, config, or `polybot.service`.

## What it sends

| Event | Alert? |
|---|---|
| `opportunity_detected` | ✅ |
| `arbitrage_detected` | ✅ |
| `paper_order_opened` | ✅ |
| `paper_order_closed` | ✅ |
| `error` | ✅ (priority) |
| `daily_report` | ✅ (summary) |
| `risk_block` | ⛔ ignored (set `WATCHER_ALERT_RISK_BLOCK=1` to enable) |
| anything else | ignored |

Messages are short fixed templates, prefixed with `[strategy]` when multi-strategy
is on, e.g.: `🎯 [aggressive] Oportunidad: <market> → BUY YES | edge 0.09 conf 0.72 <url>`.

## Robustness

Persistent byte-offset cursor keyed by inode (no duplicate alerts across restarts),
and it survives: missing file, invalid JSON lines, half-written (partial) lines,
in-place truncation, file rotation, and corrupt/missing cursor (recovers by
starting at the end — it never replays history and spams you). Undeliverable
alerts are retried then **dead-lettered** to `data/local_watcher.failed.jsonl`,
never silently lost. `Restart=always` is the final backstop.

## 1. Pull the code (existing /opt/polybot install)

```bash
cd /root/polybot-src && git pull origin claude/polymarket-bot-v1-3oqb63
sudo bash deploy/install.sh          # re-syncs /opt/polybot (no trading changes)
```

## 2. Wire your delivery channel (the ONLY integration point)

The watcher pipes each message to `WATCHER_SEND_CMD` on **stdin**. Configure it in
a git-ignored env file (so no token ever lands in the repo):

```bash
sudo -u polymarketbot cp /opt/polybot/deploy/watcher.env.example /opt/polybot/watcher.env
sudo -u polymarketbot nano /opt/polybot/watcher.env     # set WATCHER_SEND_CMD
sudo chmod 600 /opt/polybot/watcher.env                 # if it references a token
```

> **What command do I put there?** Whatever your VPS already uses to reach you.
> - If OpenClaw exposes a CLI to message you: `WATCHER_SEND_CMD=openclaw notify --to me --stdin`
> - If you have a local WhatsApp gateway: `WATCHER_SEND_CMD=curl -fsS -X POST "$URL" --data-binary @-`
> - WhatsApp Cloud API example is in `watcher.env.example`.
>
> I could not auto-detect your exact channel from outside the VPS. To find it,
> check what's installed: `command -v openclaw; systemctl list-units | grep -i whats`.
> **Until `WATCHER_SEND_CMD` is set, the watcher still runs and logs every alert
> to journald** — so nothing is lost; you just won't get WhatsApp yet.

## 3. Validate BEFORE installing the service

Confirm the main service is healthy, then dry-run the watcher against the **real**
outbox without sending or mutating anything (`--once` exits; `--dry-run` forces
stdout; it does NOT replay history, so it'll usually process 0):

```bash
systemctl status polybot --no-pager        # must be active (running)

# Safe read-only smoke test (no WhatsApp, no state change beyond cursor):
sudo -u polymarketbot WATCHER_FROM_START=1 \
  /opt/polybot/.venv/bin/python /opt/polybot/scripts/local_watcher.py --once --dry-run
```

`--dry-run` ignores your send command and prints alerts to the terminal so you can
eyeball the formatting against current events.

## 4. Install & start the systemd service

```bash
sudo cp /opt/polybot/deploy/polybot-local-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polybot-local-watcher.service
```

## 5. Verify

```bash
systemctl status polybot-local-watcher --no-pager
journalctl -u polybot-local-watcher -n 100 --no-pager
systemctl status polybot --no-pager        # confirm the trading service is STILL active
```

## Disable / remove

```bash
sudo systemctl disable --now polybot-local-watcher.service
sudo rm /etc/systemd/system/polybot-local-watcher.service
sudo systemctl daemon-reload
```

The main `polybot.service` is untouched by any of this.

## Notes

- Runs as `polymarketbot` (same as polybot) so it can read the outbox and write its
  cursor under `data/`. No extra privileges.
- Never reads, prints, or needs `.env`. It only reads `outbox.jsonl` (already
  secret-redacted by polybot) and writes its own cursor/dead-letter files.
- Resource-capped (128M RAM / 10% CPU) — it just tails a file.
- To replay the whole outbox once (e.g. first connect of WhatsApp), set
  `WATCHER_FROM_START=1` for a single `--once` run, then unset it.
