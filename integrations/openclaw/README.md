# polybot ↔ Rook / OpenClaw integration

Goal: polybot runs headless and **Rook (your OpenClaw assistant) tells you** when
it finds something. polybot stays decoupled — it just emits events; Rook decides
how to reach you (it already knows how to message you).

There are two handoff mechanisms. Pick by where Rook runs relative to polybot.

## Option A — same VPS (recommended): file outbox

polybot appends every alert to `data/outbox.jsonl` (enabled by default in
`config.yaml` → `notifications.outbox`). Rook reads it. No network, no shared
secret, survives restarts.

1. Confirm the outbox is on:
   ```yaml
   notifications:
     outbox:
       enabled: true
       path: "data/outbox.jsonl"
   ```
2. Install the skill so Rook knows what to do:
   ```bash
   cp -r integrations/openclaw/watch-polybot ~/.openclaw/skills/   # or your skills dir
   ```
3. Ask Rook: *"watch polybot and tell me when it finds opportunities."* Rook will
   tail the feed (directly or via `scripts/notifications_tail.py --json --follow`)
   and message you.

Quick manual check (what Rook effectively runs):
```bash
cd /opt/polybot
.venv/bin/python scripts/notifications_tail.py --follow
```

## Option B — Rook elsewhere: webhook

If Rook/OpenClaw runs on another host, have polybot POST events to an endpoint
Rook controls.

1. In `.env`:
   ```
   WEBHOOK_URL=https://<your-rook-endpoint>/polybot
   WEBHOOK_TOKEN=<optional-bearer-token>
   ```
2. Keep `notifications.enabled: true`. Each event is POSTed as
   `{"ts":..., "event":"<type>", "data":{...}}` with `Authorization: Bearer <token>`
   if a token is set.
3. Rook receives the POST and relays it to you.

Both options can run at once (file **and** webhook) — `Notifier` fans out to every
enabled sink.

## Event payload shape

```json
{
  "ts": "2026-06-10T10:15:02Z",
  "event": "opportunity_detected",
  "data": {
    "market": "Will ... ?",
    "outcome": "YES",
    "market_price": 0.46,
    "estimated_probability": 0.55,
    "edge": 0.09,
    "confidence": 0.7,
    "liquidity": 4200.0,
    "spread": 0.02,
    "suggested_action": "BUY YES",
    "url": "https://polymarket.com/market/..."
  }
}
```

Event types: `opportunity_detected`, `arbitrage_detected`, `paper_order_opened`,
`paper_order_closed`, `risk_block`, `live_order_created`, `live_order_filled`,
`daily_report`, `error`. Toggle each under `notifications.events` in config.

## Security

- Payloads are secret-redacted before they're written/sent.
- The outbox file lives under `data/` (git-ignored). No keys are ever in it.
- Webhook token is read from `.env` only.
