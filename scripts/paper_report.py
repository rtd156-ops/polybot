#!/usr/bin/env python3
"""Paper-trading report: PnL, win rate, open positions, exposure by category.

Usage:
    python scripts/paper_report.py
"""

from __future__ import annotations

from _common import bootstrap

from polybot.ledger import Ledger


def main() -> int:
    cfg = bootstrap()
    ledger = Ledger(cfg.db_path())
    try:
        start = float(cfg.get("paper", "starting_balance_usd", default=1000))

        closed = ledger.query(
            "SELECT realized_pnl FROM positions WHERE status='CLOSED' AND is_paper=1"
        )
        wins = sum(1 for r in closed if r["realized_pnl"] > 0)
        realized = sum(r["realized_pnl"] for r in closed)
        win_rate = (wins / len(closed) * 100) if closed else 0.0

        open_rows = ledger.open_positions(is_paper=True)
        blocked = sum(r["notional_usd"] for r in open_rows)

        fills = ledger.query(
            "SELECT COALESCE(SUM(fee_usd),0) f, COALESCE(AVG(slippage_bps),0) s, "
            "COUNT(*) c FROM fills WHERE is_paper=1"
        )[0]

        by_cat: dict[str, float] = {}
        for r in open_rows:
            by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["notional_usd"]

        print("\n=== PAPER REPORT ===")
        print(f"  starting balance : ${start:,.2f}")
        print(f"  realized PnL     : ${realized:,.2f}")
        print(f"  equity (approx)  : ${start + realized:,.2f}")
        print(f"  closed positions : {len(closed)}  (wins: {wins})")
        print(f"  win rate         : {win_rate:.1f}%")
        print(f"  open positions   : {len(open_rows)}")
        print(f"  capital blocked  : ${blocked:,.2f}")
        print(f"  fills            : {fills['c']}")
        print(f"  fees paid        : ${fills['f']:,.4f}")
        print(f"  avg slippage     : {fills['s']:.1f} bps")

        print("\n  exposure by category:")
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"    {cat:14}: ${amt:,.2f}")

        if open_rows:
            print("\n  open positions:")
            for r in open_rows:
                print(f"    {r['outcome']:3} {r['size']:>8.1f} @ {r['entry_price']:.3f}  "
                      f"${r['notional_usd']:>7.2f}  {r['market_id']}")
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
