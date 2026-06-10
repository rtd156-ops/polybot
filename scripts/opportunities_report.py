#!/usr/bin/env python3
"""Opportunities & signals report: detections, accept/reject reasons, best/worst.

Usage:
    python scripts/opportunities_report.py [--days N] [--top N]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from _common import bootstrap

from polybot.ledger import Ledger


def main() -> int:
    ap = argparse.ArgumentParser(description="Opportunities report")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args()

    cfg = bootstrap()
    ledger = Ledger(cfg.db_path())
    try:
        since = (datetime.now(tz=timezone.utc) - timedelta(days=args.days)).isoformat()

        opps = ledger.query(
            "SELECT * FROM opportunities WHERE created_at>=? ORDER BY edge DESC",
            (since,),
        )
        signals = ledger.query(
            "SELECT accepted, COUNT(*) c FROM signals WHERE created_at>=? GROUP BY accepted",
            (since,),
        )
        rejections = ledger.query(
            """SELECT reason, COUNT(*) c FROM risk_rejections WHERE created_at>=?
               GROUP BY reason ORDER BY c DESC""",
            (since,),
        )

        accepted = next((r["c"] for r in signals if r["accepted"] == 1), 0)
        rejected = next((r["c"] for r in signals if r["accepted"] == 0), 0)

        print(f"\n=== OPPORTUNITIES REPORT (last {args.days}d) ===")
        print(f"  opportunities detected : {len(opps)}")
        print(f"  signals accepted       : {accepted}")
        print(f"  signals rejected       : {rejected}")

        print("\n  rejection reasons:")
        for r in rejections:
            print(f"    {r['c']:>4}x  {r['reason']}")

        def show(rows, title):
            print(f"\n  {title}:")
            for o in rows:
                print(f"    edge={o['edge']:+.3f} conf={o['confidence']:.2f} "
                      f"{o['outcome']:3} @{o['market_price']:.3f}  {o['question'][:55]}")

        if opps:
            show(opps[: args.top], f"top {args.top} by edge")
            show(list(reversed(opps))[: args.top], f"bottom {args.top} by edge")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
