"""Strategy reporting & ranking.

Pure-ish helpers (DB in, numbers out) so the comparison logic is unit-testable
without the CLI. Used by scripts/strategy_compare.py to rank conservative vs
balanced vs aggressive once enough paper data has accumulated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

from .ledger import Ledger


def compute_max_drawdown(equity_series: list[float]) -> float:
    """Max peak-to-trough drop over an equity series, as a fraction (0..1)."""
    peak = float("-inf")
    max_dd = 0.0
    for e in equity_series:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)
    return max_dd


def risk_adjusted_score(return_pct: float, max_drawdown_pct: float) -> float:
    """Calmar-like: return per unit of drawdown. Floors DD so it's well-defined.

    Higher is better. A strategy that made 8% with 2% DD (score 4.0) beats one
    that made 10% with 20% DD (score 0.5).
    """
    return return_pct / max(max_drawdown_pct, 1.0)


@dataclass
class StrategyMetrics:
    strategy_id: str
    starting_balance: float
    realized_pnl: float
    equity: float
    return_pct: float
    closed_trades: int
    wins: int
    win_rate: float
    open_positions: int
    blocked_usd: float
    max_drawdown_pct: float
    total_fees: float
    avg_slippage_bps: float
    opportunities: int
    accepted: int
    rejected: int
    acceptance_rate: float
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def strategy_metrics(
    ledger: Ledger,
    strategy_id: str,
    starting_balance: float,
    since: Optional[str] = None,
) -> StrategyMetrics:
    """Compute the full metric set for one strategy from the ledger."""
    since_clause = " AND created_at>=?" if since else ""
    sp = (strategy_id, since) if since else (strategy_id,)

    closed = ledger.query(
        "SELECT realized_pnl FROM positions WHERE status='CLOSED' AND is_paper=1 "
        "AND strategy_id=?" + (" AND closed_at>=?" if since else ""), sp,
    )
    realized = sum(r["realized_pnl"] for r in closed)
    wins = sum(1 for r in closed if r["realized_pnl"] > 0)
    win_rate = (wins / len(closed) * 100) if closed else 0.0

    open_rows = ledger.open_positions(is_paper=True, strategy_id=strategy_id)
    blocked = sum(r["notional_usd"] for r in open_rows)

    fills = ledger.query(
        "SELECT COALESCE(SUM(fee_usd),0) f, COALESCE(AVG(slippage_bps),0) s "
        "FROM fills WHERE is_paper=1 AND strategy_id=?", (strategy_id,),
    )[0]

    opps = ledger.query(
        "SELECT COUNT(*) c FROM opportunities WHERE strategy_id=?" + since_clause, sp,
    )[0]["c"]
    sig_rows = ledger.query(
        "SELECT accepted, COUNT(*) c FROM signals WHERE strategy_id=?" + since_clause
        + " GROUP BY accepted", sp,
    )
    accepted = next((r["c"] for r in sig_rows if r["accepted"] == 1), 0)
    rejected = next((r["c"] for r in sig_rows if r["accepted"] == 0), 0)
    total_sig = accepted + rejected
    acceptance = (accepted / total_sig * 100) if total_sig else 0.0

    snap_rows = ledger.query(
        "SELECT equity_usd FROM balance_snapshots WHERE is_paper=1 AND strategy_id=? "
        "ORDER BY created_at", (strategy_id,),
    )
    equity_series = [r["equity_usd"] for r in snap_rows]
    max_dd = compute_max_drawdown(equity_series) * 100.0

    equity = starting_balance + realized
    return_pct = (realized / starting_balance * 100) if starting_balance else 0.0

    return StrategyMetrics(
        strategy_id=strategy_id,
        starting_balance=round(starting_balance, 2),
        realized_pnl=round(realized, 2),
        equity=round(equity, 2),
        return_pct=round(return_pct, 3),
        closed_trades=len(closed),
        wins=wins,
        win_rate=round(win_rate, 1),
        open_positions=len(open_rows),
        blocked_usd=round(blocked, 2),
        max_drawdown_pct=round(max_dd, 2),
        total_fees=round(fills["f"], 4),
        avg_slippage_bps=round(fills["s"], 1),
        opportunities=opps,
        accepted=accepted,
        rejected=rejected,
        acceptance_rate=round(acceptance, 1),
        score=round(risk_adjusted_score(return_pct, max_dd), 3),
    )


_SORT_KEYS = {
    "pnl": lambda m: m.realized_pnl,
    "return": lambda m: m.return_pct,
    "winrate": lambda m: m.win_rate,
    "score": lambda m: m.score,
    "drawdown": lambda m: -m.max_drawdown_pct,  # lower DD ranks higher
}


def rank_strategies(metrics: list[StrategyMetrics], key: str = "score") -> list[StrategyMetrics]:
    """Return metrics sorted best-first by the given key."""
    keyfn = _SORT_KEYS.get(key, _SORT_KEYS["score"])
    return sorted(metrics, key=keyfn, reverse=True)
