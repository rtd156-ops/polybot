from __future__ import annotations

from polybot.models import (
    Opportunity,
    Outcome,
    PricingSnapshot,
    ProbabilityEstimate,
)
from polybot.risk import RiskManager, RiskState


def _opp(sample_market, spread=0.02, liquidity=80000.0):
    sample_market.liquidity_usd = liquidity
    snap = PricingSnapshot(
        market_id=sample_market.market_id, token_id="tok-yes", outcome=Outcome.YES,
        best_bid=0.44, best_ask=0.46, mid=0.45, spread=spread,
        implied_probability=0.45, useful_liquidity_usd=4000.0,
    )
    est = ProbabilityEstimate(0.55, 0.45, 0.10, 0.7, ["edge"])
    return Opportunity(market=sample_market, outcome=Outcome.YES, pricing=snap,
                       estimate=est, suggested_action="BUY YES")


def test_accepts_within_limits(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market), RiskState())
    assert sig.accepted and sig.notional_usd > 0 and sig.side is not None


def test_rejects_wide_spread(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, spread=0.20), RiskState())
    assert not sig.accepted and "spread_too_wide" in sig.reason


def test_rejects_low_liquidity(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, liquidity=100.0), RiskState())
    assert not sig.accepted and "liquidity_too_low" in sig.reason


def test_per_market_limit(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(exposure_by_market={sample_market.market_id: 95.0})
    sig = rm.evaluate(_opp(sample_market), state)  # 95 + 25 > 100 cap
    assert not sig.accepted and "per_market_limit" in sig.reason


def test_daily_loss_circuit_breaker(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(realized_pnl_today=-200.0)  # past -150 limit
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "daily_loss_limit_hit" in sig.reason


def test_max_new_positions_per_day(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(new_positions_today=10)
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "max_new_positions_per_day" in sig.reason


def test_total_exposure_limit(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(total_exposure_usd=990.0)  # 990 + 25 > 1000
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "total_exposure_limit" in sig.reason
