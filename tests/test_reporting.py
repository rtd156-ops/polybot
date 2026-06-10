from __future__ import annotations

from polybot.ledger import Ledger
from polybot.models import Market, OrderResult, OrderSide, OrderStatus, Outcome
from polybot.reporting import (
    compute_max_drawdown,
    rank_strategies,
    risk_adjusted_score,
    strategy_metrics,
)


# --- pure helpers ------------------------------------------------------------

def test_max_drawdown_basic():
    # peak 120, trough 90 -> (120-90)/120 = 25%
    assert abs(compute_max_drawdown([100, 120, 90, 110]) - 0.25) < 1e-9


def test_max_drawdown_monotonic_up_is_zero():
    assert compute_max_drawdown([100, 110, 120]) == 0.0


def test_max_drawdown_empty():
    assert compute_max_drawdown([]) == 0.0


def test_risk_adjusted_score_prefers_low_drawdown():
    a = risk_adjusted_score(8.0, 2.0)    # 8% return, 2% DD -> 4.0
    b = risk_adjusted_score(10.0, 20.0)  # 10% return, 20% DD -> 0.5
    assert a > b


def test_score_floors_drawdown():
    # tiny/zero DD must not explode the score (floored at 1.0)
    assert risk_adjusted_score(5.0, 0.0) == 5.0


# --- end-to-end metrics + ranking from a ledger ------------------------------

def _market():
    return Market("m1", "0x", "Q?", "s", "Crypto", True, False, 5e5, 8e4,
                  "2026-09-01T00:00:00Z", "ty", "tn", 0.45, 0.55, "u")


def _order(oid, notional, price=0.5):
    return OrderResult(oid, OrderStatus.FILLED, "m1", "ty", Outcome.YES, OrderSide.BUY,
                       price, notional / price, notional / price, notional, True,
                       fee_usd=0.05, slippage_bps=20)


def _winning_closed(led, sid, notional, exit_price):
    led.record_order(_order(sid + "o", notional), strategy_id=sid)
    led.record_fill(_order(sid + "o", notional), strategy_id=sid)
    pid = led.open_position(market=_market(), order=_order(sid + "o", notional), strategy_id=sid)
    led.close_position(pid, exit_price)


def test_strategy_metrics_from_ledger(tmp_path):
    led = Ledger(tmp_path / "l.db")
    # balanced: one winning closed trade (entry 0.5 -> 0.6 on $25 notional)
    _winning_closed(led, "balanced", 25, 0.60)
    led.snapshot_balance(cash=2005, blocked=0, unrealized=0, realized=5,
                         is_paper=True, strategy_id="balanced")

    m = strategy_metrics(led, "balanced", starting_balance=2000)
    assert m.closed_trades == 1 and m.wins == 1 and m.win_rate == 100.0
    assert m.realized_pnl > 0
    assert m.equity == 2000 + m.realized_pnl
    assert m.total_fees == 0.05
    assert m.avg_slippage_bps == 20.0
    led.close()


def test_ranking_orders_best_first(tmp_path):
    led = Ledger(tmp_path / "l.db")
    # aggressive makes more $ but we'll give it a worse drawdown via snapshots
    _winning_closed(led, "conservative", 10, 0.60)   # +$2 on $10
    _winning_closed(led, "aggressive", 50, 0.60)     # +$10 on $50

    # conservative: smooth equity (no DD); aggressive: big dip then recovery
    for e in [1000, 1001, 1002]:
        led.snapshot_balance(cash=e, blocked=0, unrealized=0, realized=0,
                             is_paper=True, strategy_id="conservative")
    for e in [3000, 2400, 3010]:  # 20% drawdown
        led.snapshot_balance(cash=e, blocked=0, unrealized=0, realized=0,
                             is_paper=True, strategy_id="aggressive")

    cons = strategy_metrics(led, "conservative", 1000)
    aggr = strategy_metrics(led, "aggressive", 3000)

    by_pnl = rank_strategies([cons, aggr], key="pnl")
    assert by_pnl[0].strategy_id == "aggressive"   # bigger absolute PnL

    by_drawdown = rank_strategies([cons, aggr], key="drawdown")
    assert by_drawdown[0].strategy_id == "conservative"  # lower DD ranks first

    assert aggr.max_drawdown_pct >= 19.0  # ~20%
    led.close()


def test_metrics_handle_no_data(tmp_path):
    led = Ledger(tmp_path / "l.db")
    m = strategy_metrics(led, "conservative", 1000)
    assert m.closed_trades == 0 and m.realized_pnl == 0.0
    assert m.max_drawdown_pct == 0.0 and m.score == 0.0
    led.close()
