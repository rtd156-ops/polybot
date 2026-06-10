# polybot

A professional, **safety-first** Polymarket bot. V1 ships **scanner + analysis +
paper trading**. Live execution is fully designed but **gated OFF** by default —
the architecture lets you turn it on later without rewriting anything.

Maturity path the codebase is built for:

1. **scanner + alerts** ✅
2. **paper trading** ✅
3. semi-automatic execution *(interface ready)*
4. controlled real execution *(gated, stubbed in V1)*

---

## Why this design

- **Public data clients vs. private trading client are strictly separated.**
  `clients/gamma.py` and `clients/clob.py` are read-only and unauthenticated.
  The only code that can move money is `execution/live.py`, behind a hard gate.
- **One execution interface** (`ExecutionClient`) → `PaperExecution` today,
  `PolymarketLiveExecution` tomorrow. The engine never knows the difference.
- **Everything is auditable** in SQLite: markets, opportunities, signals, paper
  & live orders, positions, fills, risk rejections, balance snapshots, events.
- **Secrets never touch git or logs.** `.env` only; a redaction filter scrubs
  every log line and webhook payload.
- **Conservative by construction.** Paper fills pay the far side of the book,
  not the mid. Nothing is "tradeable" unless you can both enter and exit.

## Architecture

```
Gamma API ──► scanner ──► pricing ──► probability ──► risk ──► execution ──► ledger
 (public)     (filter)    (CLOB book)  (heuristic)   (limits)  (paper/live)  (SQLite)
                                                                    │
                                                              notifications (webhook)
```

| Module | Responsibility |
|---|---|
| `config.py` | merge `config.yaml` ⊕ `config.local.yaml` ⊕ `.env`; live safety gate |
| `clients/gamma.py` | fetch + normalize markets (public) |
| `clients/clob.py` | fetch order books / prices (public) |
| `scanner.py` | filter by volume, liquidity, spread, category, resolution date |
| `pricing.py` | mid, spread, implied prob, *useful* liquidity, signals |
| `probability.py` | heuristic estimate → edge, confidence, reasons (extensible) |
| `risk.py` | per-market/category/total limits, daily caps, reject reasons |
| `execution/paper.py` | conservative simulated fills |
| `execution/live.py` | real CLOB orders — **disabled** |
| `ledger.py` | SQLite system of record |
| `notifications.py` | webhook alerts (redacted) |
| `engine.py` | orchestrates a cycle / the loop |

## Robustness features (distilled from the best public bots)

These patterns were taken from the official Polymarket keeper, OctoBot, the
Octagon risk engine, and the agent-next paper trader (the legit, vetted ones):

- **Realistic fills** — paper orders *walk the real order book* level-by-level
  (taker model), model Polymarket's fee formula `bps/10000 × min(p,1-p) × shares`,
  and record slippage vs mid in bps. Partial fills when the book is too thin.
- **Fractional-Kelly sizing** — stake = `½ × edge/(1-price) × bankroll`, capped at
  10% of bankroll. Trims to remaining exposure headroom instead of rejecting.
  (`risk.use_kelly`, falls back to a fixed ticket.)
- **Drawdown circuit breaker** — halts new entries past `risk.max_drawdown_pct`
  (peak-to-current equity), on top of the daily-loss limit.
- **Price-band gate** — refuses entries below 5c / above 95c where edge is usually
  illusory.
- **Graceful shutdown** — SIGTERM/SIGINT finish the current cycle and close the
  ledger cleanly (matters for `systemctl stop/restart`).
- **Rate limiting** — token-bucket caps outbound API calls (`data_sources.rate_limit_per_sec`).
- **Order reconciliation** — pure diff of desired vs open orders (cancel/place the
  delta only); wired into the gated live client.
- **Risk-free arbitrage** — optional detector for YES+NO < $1 (`arbitrage.enabled`).
- **WebSocket order book** — opt-in reconnecting stream (`clients/ws.py`) for
  low-latency strategies; REST polling remains the default.

## Operating modes

- `analysis` — scan, score, alert. **No orders**, not even simulated.
- `paper` — analysis + simulated orders/fills/PnL persisted to SQLite.
- `live` — real orders. Requires `mode: live` **and** `execution.live_enabled:
  true` **and** CLOB credentials, or the bot refuses to start.

## Quick start (local)

```bash
# 1. Install runtime deps
pip install -r requirements.txt

# 2. (optional) configure alerts
cp .env.example .env            # set WEBHOOK_URL / WEBHOOK_TOKEN if you want
cp config.local.yaml.example config.local.yaml   # tweak host settings

# 3. Scan real markets (read-only, no DB writes)
python scripts/scan_markets.py --show-rejected

# 4. Run one analysis/paper cycle (persists to data/polybot.db)
python scripts/run_once.py --mode paper

# 5. Reports
python scripts/paper_report.py
python scripts/opportunities_report.py --days 7
```

## Run the tests

```bash
pip install pytest
python -m pytest -q          # 44 tests, fully offline (fixtures, no network)
```

## Configuration

`config.yaml` is the versioned base (documented inline). Override per-host values
in `config.local.yaml` (git-ignored) — it deep-merges on top. Secrets come from
`.env` only. Precedence: **CLI flags > config.local.yaml > config.yaml >
defaults**.

Key knobs: `scanner.*` (filters), `pricing.*` (signal thresholds),
`probability.*` (edge/confidence gates + weights), `risk.*` (exposure & daily
limits), `paper.*` (starting balance, stake, slippage).

## Extending the probability model

`probability.ProbabilityModel.estimate` returns a stable `ProbabilityEstimate`
(`estimated_probability`, `market_probability`, `edge`, `confidence`, `reasons`).
Subclass it to plug in ML, news, or LLM analysis and swap it into the engine —
nothing downstream changes.

## Getting alerts (and wiring Rook / OpenClaw)

polybot emits every event to any enabled sink, redacted:

- **File outbox** (`data/outbox.jsonl`, on by default) — an append-only JSONL feed
  an external assistant can tail. Stream it with
  `python scripts/notifications_tail.py --follow` (a cursor avoids repeats).
- **Webhook** — set `WEBHOOK_URL`/`WEBHOOK_TOKEN` in `.env` to POST events to any
  endpoint (Slack/Discord/n8n/your own relay).

To have **Rook (OpenClaw)** notify you, install the bundled skill and point it at
the outbox (same VPS) or the webhook (remote). See
[integrations/openclaw/README.md](integrations/openclaw/README.md).

## VPS / 24-7 deployment

See **[deploy/DEPLOY.md](deploy/DEPLOY.md)** for the full Ubuntu/Debian guide:
dedicated user, systemd unit (auto-restart, resource caps, journald logs),
permissions (`chmod 600 .env`), reports, and the deliberate path to enable live.

## Security notes

- No private keys, API keys, tokens or signatures are stored in git or printed.
- `.gitignore` excludes `.env`, `config.local.yaml`, `*.db`, logs.
- Live execution is inert in V1 (`LiveExecutionDisabled`) until you wire and
  enable it on a secured host.

## Status

**V1 = analysis + paper.** Live execution is intentionally not wired. Validate
opportunity quality and paper behavior first; add the wallet and real orders
only once the numbers justify it.
