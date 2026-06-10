#!/usr/bin/env python3
"""Rank strategies head-to-head: PnL, return, win rate, drawdown, risk-adjusted.

Run this once the paper books have accumulated enough closed trades to be
meaningful. It ranks conservative/balanced/aggressive so you can decide which
one (if any) is worth promoting to live -- later, deliberately.

Usage:
    python scripts/strategy_compare.py                 # rank by risk-adjusted score
    python scripts/strategy_compare.py --sort pnl      # or: return|winrate|drawdown
    python scripts/strategy_compare.py --days 30       # window the activity metrics
    python scripts/strategy_compare.py --json          # machine-readable (for Rook)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from _common import bootstrap

from polybot.ledger import Ledger
from polybot.reporting import rank_strategies, strategy_metrics
from polybot.strategies import load_strategies

# Below this many closed trades per strategy, treat the ranking as not yet
# statistically meaningful and say so loudly.
_MIN_TRADES_FOR_CONFIDENCE = 20


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare & rank paper strategies")
    ap.add_argument("--sort", choices=["score", "pnl", "return", "winrate", "drawdown"],
                    default="score")
    ap.add_argument("--days", type=int, default=None, help="window activity metrics")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    cfg = bootstrap()
    ledger = Ledger(cfg.db_path())
    try:
        configured = {
            s.strategy_id: float(s.config.get("paper", "starting_balance_usd", default=1000))
            for s in load_strategies(cfg)
        }
        # Include any strategies present in the DB but not currently configured.
        for sid in ledger.strategy_ids():
            if sid not in configured and sid != "arbitrage":
                configured[sid] = float(cfg.get("paper", "starting_balance_usd", default=1000))

        since = None
        if args.days:
            since = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).isoformat()

        metrics = [strategy_metrics(ledger, sid, bal, since=since)
                   for sid, bal in configured.items()]
        ranked = rank_strategies(metrics, key=args.sort)

        if args.json:
            print(json.dumps([m.to_dict() for m in ranked], indent=2))
            return 0

        _print_table(ranked, args.sort)
    finally:
        ledger.close()
    return 0


def _print_table(ranked, sort_key: str) -> None:
    window = ""
    print(f"\n=== STRATEGY RANKING (by {sort_key}){window} ===\n")
    hdr = (f"{'#':<2} {'strategy':<13} {'equity':>9} {'return%':>8} {'PnL':>9} "
           f"{'trades':>6} {'win%':>6} {'maxDD%':>7} {'score':>7} {'fees':>7}")
    print(hdr)
    print("-" * len(hdr))
    total_trades = 0
    for i, m in enumerate(ranked, 1):
        total_trades += m.closed_trades
        print(f"{i:<2} {m.strategy_id:<13} {m.equity:>9,.0f} {m.return_pct:>8.2f} "
              f"{m.realized_pnl:>9,.2f} {m.closed_trades:>6} {m.win_rate:>6.1f} "
              f"{m.max_drawdown_pct:>7.2f} {m.score:>7.2f} {m.total_fees:>7.2f}")

    print("\nactivity (opportunities / accepted / rejected):")
    for m in ranked:
        print(f"  {m.strategy_id:<13} opps={m.opportunities:<5} "
              f"accepted={m.accepted:<4} rejected={m.rejected:<4} "
              f"(accept {m.acceptance_rate:.0f}%)  open={m.open_positions} "
              f"blocked=${m.blocked_usd:,.0f}")

    if ranked:
        best = ranked[0]
        print(f"\nLeader by {sort_key}: [{best.strategy_id}]")
        if total_trades < _MIN_TRADES_FOR_CONFIDENCE:
            print(f"  ⚠  Only {total_trades} closed trades total -- NOT yet "
                  f"statistically meaningful. Let it run longer before deciding.")
        else:
            print("  Enough sample to start comparing; still confirm over more time "
                  "and across different market regimes before considering live.")


if __name__ == "__main__":
    raise SystemExit(main())
