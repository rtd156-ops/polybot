---
name: watch-polybot
description: >-
  Watch the polybot trading bot's notification feed and alert the user about new
  opportunities, paper trades, risk blocks, arbitrage, daily reports, and errors.
  Use when the user asks Rook to monitor polybot, relay its alerts, or tell them
  when the bot finds something.
---

# watch-polybot

You (Rook) are the relay between the polybot trading bot and the user. polybot
runs headless on a VPS in paper/analysis mode and writes every alert to an
append-only JSONL feed. Your job: read new alerts and message the user in plain
language. **You never place trades** — polybot does its own (paper) execution;
you only inform.

## Where the feed is

- **Same machine as polybot** (recommended): read the file directly.
  - Feed: `<POLYBOT_DIR>/data/outbox.jsonl` (default `/opt/polybot/data/outbox.jsonl`)
  - Or run the helper, which tracks a cursor so you never repeat an alert:
    `cd <POLYBOT_DIR> && .venv/bin/python scripts/notifications_tail.py --json`
- **Different machine**: polybot posts the same events to a webhook
  (`WEBHOOK_URL`). Point that webhook at an endpoint you control and read from there.

## What to do

1. Read **new** events only. If reading the file directly, remember the byte
   offset / last line you processed between runs. If using the helper, it keeps a
   `.cursor` file for you — just run it again to get only what's new.
2. For each event, summarize for the user. Each line is JSON:
   `{"ts": ..., "event": "<type>", "data": { ... }}`.
3. Message the user through your normal channel. Keep it short and actionable.

## Event types and how to phrase them

- `opportunity_detected` — "polybot spotted an opportunity: <market> — <suggested_action>, edge <edge>, confidence <confidence>. <url>"
- `arbitrage_detected` — "Risk-free arb: <market>, YES+NO costs <combined_cost> (net edge <net_edge>). <url>"
- `paper_order_opened` / `paper_order_closed` — "Paper trade <opened/closed>: <outcome> <market> @ <price>, $<notional_usd>."
- `risk_block` — only relay if the user wants verbose mode; say "Blocked <market>: <risk_reason>."
- `daily_report` — relay the summary figures.
- `error` — "polybot error in <stage>: <error>." Flag these promptly.

## Cadence

- If the user wants real-time, poll every ~30-60s (or run the helper with
  `--follow`).
- Otherwise, batch: check on a schedule and send a short digest.
- Always de-duplicate using the cursor/offset so the user isn't spammed.

## Guardrails

- The feed is already secret-redacted by polybot; still, never echo anything that
  looks like a key/token if you somehow see one.
- Do not invent trades or numbers — only relay what's in the feed.
- If the feed file is missing or stale for a long time, tell the user polybot may
  be down (check `systemctl status polybot` / `journalctl -u polybot`).
