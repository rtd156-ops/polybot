#!/usr/bin/env python3
"""Scan Polymarket and print markets that pass the configured filters.

Read-only: no orders, no DB writes. Good for tuning scanner thresholds.

Usage:
    python scripts/scan_markets.py [--limit N] [--show-rejected]
"""

from __future__ import annotations

import argparse

from _common import bootstrap

from polybot.clients.gamma import GammaClient
from polybot.clients.http import HttpClient
from polybot.scanner import MarketScanner


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan Polymarket markets")
    ap.add_argument("--limit", type=int, default=None, help="max markets to pull")
    ap.add_argument("--show-rejected", action="store_true", help="list rejected too")
    args = ap.parse_args()

    cfg = bootstrap()
    if args.limit:
        cfg.data["scanner"]["max_markets_scanned"] = args.limit

    http = HttpClient(
        timeout=float(cfg.get("data_sources", "http_timeout_seconds", default=15)),
        max_retries=int(cfg.get("data_sources", "http_max_retries", default=3)),
        backoff=float(cfg.get("data_sources", "http_backoff_seconds", default=1.5)),
        user_agent=str(cfg.get("data_sources", "user_agent", default="polybot/1.0")),
    )
    scanner = MarketScanner(cfg, GammaClient(cfg.get("data_sources", "gamma_base_url"), http))
    passing, results = scanner.scan()

    print(f"\n=== {len(passing)} markets passed / {len(results)} scanned ===\n")
    for m in passing:
        print(f"[{m.category or '-':12}] vol=${m.volume_usd:>10,.0f} "
              f"liq=${m.liquidity_usd:>9,.0f}  {m.question[:70]}")
        print(f"               {m.url}")

    if args.show_rejected:
        print("\n--- rejected ---")
        for r in results:
            if not r.passed:
                print(f"  [{r.reason:32}] {r.market.question[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
