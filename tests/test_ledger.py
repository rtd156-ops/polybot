from __future__ import annotations

from polybot.ledger import Ledger
from polybot.models import (
    Market,
    Opportunity,
    OrderResult,
    OrderSide,
    OrderStatus,
    Outcome,
    PricingSnapshot,
    ProbabilityEstimate,
    Signal,
)


def _market():
    return Market("123", "0xc", "Q?", "slug", "Crypto", True, False,
                  500000, 80000, "2026-09-01T00:00:00Z", "ty", "tn", 0.45, 0.55,
                  "https://polymarket.com/market/slug")


def _opp(m):
    snap = PricingSnapshot(m.market_id, "ty", Outcome.YES, 0.44, 0.46, 0.45, 0.02,
                           0.45, 4000.0)
    est = ProbabilityEstimate(0.55, 0.45, 0.10, 0.7, ["r"])
    return Opportunity(m, Outcome.YES, snap, est, "BUY YES")


def test_persistence_survives_reopen(tmp_path):
    db = tmp_path / "l.db"
    m = _market()
    led = Ledger(db)
    led.upsert_market(m)
    led.record_opportunity(_opp(m))
    led.close()

    led2 = Ledger(db)
    rows = led2.query("SELECT * FROM opportunities")
    assert len(rows) == 1 and rows[0]["market_id"] == "123"
    assert led2.query("SELECT * FROM markets")[0]["liquidity_usd"] == 80000
    led2.close()


def test_signal_rejection_logged(tmp_path):
    led = Ledger(tmp_path / "l.db")
    m = _market()
    sig = Signal(opportunity=_opp(m), accepted=False, reason="per_market_limit(...)")
    led.record_signal(sig)
    rej = led.query("SELECT * FROM risk_rejections")
    assert len(rej) == 1 and "per_market_limit" in rej[0]["reason"]
    led.close()


def test_position_lifecycle_and_pnl(tmp_path):
    led = Ledger(tmp_path / "l.db")
    m = _market()
    order = OrderResult(
        order_id="paper-1", status=OrderStatus.FILLED, market_id="123",
        token_id="ty", outcome=Outcome.YES, side=OrderSide.BUY, price=0.50,
        size=50.0, filled_size=50.0, notional_usd=25.0, is_paper=True,
    )
    led.record_order(order)
    led.record_fill(order)
    pid = led.open_position(market=m, order=order)

    inputs = led.risk_state_inputs(is_paper=True)
    assert inputs["total_exposure_usd"] == 25.0
    assert inputs["new_positions_today"] == 1

    pnl = led.close_position(pid, exit_price=0.60)
    assert abs(pnl - (0.60 - 0.50) * 50.0) < 1e-9

    closed = led.query("SELECT * FROM positions WHERE status='CLOSED'")
    assert len(closed) == 1
    led.close()


def test_event_audit(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record_event("opportunity_detected", {"market": "Q?"}, severity="info")
    rows = led.query("SELECT * FROM events")
    assert rows[0]["event_type"] == "opportunity_detected"
    led.close()
