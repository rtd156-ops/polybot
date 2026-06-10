from __future__ import annotations

from polybot.models import (
    Opportunity,
    Outcome,
    PricingSnapshot,
    ProbabilityEstimate,
)
from polybot.risk import RiskManager, RiskState, kelly_fraction


def _opp(sample_market, spread=0.02, liquidity=80000.0, est_prob=0.55, best_ask=0.46):
    sample_market.liquidity_usd = liquidity
    snap = PricingSnapshot(
        market_id=sample_market.market_id, token_id="tok-yes", outcome=Outcome.YES,
        best_bid=best_ask - 0.02, best_ask=best_ask, mid=best_ask - 0.01, spread=spread,
        implied_probability=best_ask - 0.01, useful_liquidity_usd=4000.0,
    )
    est = ProbabilityEstimate(est_prob, best_ask - 0.01, est_prob - (best_ask - 0.01),
                              0.7, ["edge"])
    return Opportunity(market=sample_market, outcome=Outcome.YES, pricing=snap,
                       estimate=est, suggested_action="BUY YES")


# --- Kelly sizing math -------------------------------------------------------

def test_kelly_fraction_positive_edge():
    # q=0.6, p=0.5 -> (0.6-0.5)/(1-0.5) = 0.2
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9


def test_kelly_fraction_no_edge_is_zero():
    assert kelly_fraction(0.40, 0.50) == 0.0  # negative edge -> never size


def test_kelly_used_for_sizing(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, est_prob=0.55, best_ask=0.46), RiskState())
    # half-Kelly of (0.55-0.46)/0.54=0.1667 -> 0.0833 of $1000 bankroll ~ $83
    assert sig.accepted
    assert 70 < sig.notional_usd < 95


def test_kelly_capped_by_max_fraction(base_config, sample_market):
    rm = RiskManager(base_config)
    # Huge edge would blow past the 10% cap; ensure it's clamped (<= $100).
    sig = rm.evaluate(_opp(sample_market, est_prob=0.95, best_ask=0.40), RiskState())
    assert sig.accepted and sig.notional_usd <= 100.0


def test_fixed_sizing_when_kelly_disabled(base_config, sample_market):
    base_config.data["risk"]["use_kelly"] = False
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market), RiskState())
    assert sig.accepted and sig.notional_usd == 25.0  # default_order_notional_usd


# --- Quality gates -----------------------------------------------------------

def test_rejects_wide_spread(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, spread=0.20), RiskState())
    assert not sig.accepted and "spread_too_wide" in sig.reason


def test_rejects_low_liquidity(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, liquidity=100.0), RiskState())
    assert not sig.accepted and "liquidity_too_low" in sig.reason


def test_rejects_price_out_of_band(base_config, sample_market):
    rm = RiskManager(base_config)
    sig = rm.evaluate(_opp(sample_market, best_ask=0.98), RiskState())
    assert not sig.accepted and "price_out_of_band" in sig.reason


# --- Exposure limits (Kelly trims to headroom; rejects when exhausted) ------

def test_trims_to_remaining_headroom(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(exposure_by_market={sample_market.market_id: 60.0})
    sig = rm.evaluate(_opp(sample_market), state)
    # 100 cap - 60 used = 40 headroom; Kelly(~83) trimmed down to 40.
    assert sig.accepted and sig.notional_usd <= 40.0


def test_rejects_when_market_cap_exhausted(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(exposure_by_market={sample_market.market_id: 100.0})
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "per_market_limit" in sig.reason


def test_rejects_when_total_exposure_exhausted(base_config, sample_market):
    rm = RiskManager(base_config)
    state = RiskState(total_exposure_usd=1000.0)
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "total_exposure_limit" in sig.reason


# --- Circuit breakers --------------------------------------------------------

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


def test_max_drawdown_breaker(base_config, sample_market):
    rm = RiskManager(base_config)
    # peak 1000, current 750 -> 25% DD >= 20% limit.
    state = RiskState(peak_equity_usd=1000.0, current_equity_usd=750.0)
    sig = rm.evaluate(_opp(sample_market), state)
    assert not sig.accepted and "max_drawdown_hit" in sig.reason
