#!/usr/bin/env python3
"""Paper-trading report: PnL, equity, exposure, trades, rejections -- per strategy.

When multi-strategy is enabled, each strategy is reported as its own book so you
can compare conservative vs balanced vs aggressive side by side. In single mode
it shows just the "default" book. Backward compatible with old databases.

Usage:
    python scripts/paper_report.py
"""

from __future__ import annotations

from _common import bootstrap

from polybot.ledger import Ledger
from polybot.strategies import load_strategies


def _report_strategy(ledger: Ledger, sid: str, starting: float) -> None:
    closed = ledger.query(
        "SELECT realized_pnl FROM positions WHERE status='CLOSED' AND is_paper=1 "
        "AND strategy_id=?", (sid,),
    )
    wins = sum(1 for r in closed if r["realized_pnl"] > 0)
    realized = sum(r["realized_pnl"] for r in closed)
    win_rate = (wins / len(closed) * 100) if closed else 0.0

    open_rows = ledger.open_positions(is_paper=True, strategy_id=sid)
    blocked = sum(r["notional_usd"] for r in open_rows)

    by_cat: dict[str, float] = {}
    for r in open_rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["notional_usd"]

    fills = ledger.query(
        "SELECT COALESCE(SUM(fee_usd),0) f, COALESCE(AVG(slippage_bps),0) s, "
        "COUNT(*) c FROM fills WHERE is_paper=1 AND strategy_id=?", (sid,),
    )[0]
    opps = ledger.query(
        "SELECT COUNT(*) c FROM opportunities WHERE strategy_id=?", (sid,))[0]["c"]
    rejections = ledger.query(
        "SELECT reason, COUNT(*) c FROM risk_rejections WHERE strategy_id=? "
        "GROUP BY reason ORDER BY c DESC", (sid,),
    )
    n_rej = sum(r["c"] for r in rejections)

    print(f"\n=== [{sid}] ===")
    print(f"  starting balance : ${starting:,.2f}")
    print(f"  realized PnL     : ${realized:,.2f}")
    print(f"  equity (approx)  : ${starting + realized:,.2f}")
    print(f"  opportunities    : {opps}")
    print(f"  closed positions : {len(closed)}  (wins: {wins}, win rate: {win_rate:.1f}%)")
    print(f"  open positions   : {len(open_rows)}")
    print(f"  capital blocked  : ${blocked:,.2f}")
    print(f"  fills            : {fills['c']}   fees: ${fills['f']:,.4f}   "
          f"avg slip: {fills['s']:.1f} bps")
    print(f"  risk rejections  : {n_rej}")
    if by_cat:
        print("  exposure by category:")
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"    {cat:14}: ${amt:,.2f}")
    if rejections:
        print("  top rejection reasons:")
        for r in rejections[:5]:
            print(f"    {r['c']:>3}x  {r['reason']}")


def main() -> int:
    cfg = bootstrap()
    ledger = Ledger(cfg.db_path())
    try:
        # Configured strategies (id -> starting balance) plus any seen in the DB.
        configured = {
            s.strategy_id: float(s.config.get("paper", "starting_balance_usd", default=1000))
            for s in load_strategies(cfg)
        }
        ids = list(configured)
        for sid in ledger.strategy_ids():
            if sid not in configured and sid != "arbitrage":
                ids.append(sid)

        print("=== PAPER REPORT (per strategy) ===")
        for sid in ids:
            starting = configured.get(sid, float(
                cfg.get("paper", "starting_balance_usd", default=1000)))
            _report_strategy(ledger, sid, starting)
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
