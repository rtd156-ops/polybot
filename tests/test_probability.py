from __future__ import annotations

from polybot.models import Outcome
from polybot.pricing import build_snapshot, is_tradeable
from polybot.probability import ProbabilityModel, passes_thresholds


def test_snapshot_computes_mid_and_spread(base_config, sample_market, healthy_book):
    snap = build_snapshot(base_config, sample_market, Outcome.YES, healthy_book)
    assert snap.best_bid == 0.44
    assert snap.best_ask == 0.46
    assert abs(snap.mid - 0.45) < 1e-9
    assert abs(snap.spread - 0.02) < 1e-9
    assert snap.useful_liquidity_usd > 0


def test_tradeable_true_for_healthy(base_config, sample_market, healthy_book):
    snap = build_snapshot(base_config, sample_market, Outcome.YES, healthy_book)
    assert is_tradeable(base_config, snap)


def test_not_tradeable_without_book(base_config, sample_market):
    snap = build_snapshot(base_config, sample_market, Outcome.YES, None)
    assert not is_tradeable(base_config, snap)


def test_model_outputs_contract(base_config, sample_market, healthy_book):
    snap = build_snapshot(base_config, sample_market, Outcome.YES, healthy_book)
    model = ProbabilityModel(base_config)
    est = model.estimate(sample_market, Outcome.YES, snap, healthy_book)
    assert est is not None
    assert 0 <= est.estimated_probability <= 1
    assert 0 <= est.confidence <= 1
    assert est.market_probability == round(snap.mid, 4)
    assert est.reasons  # always explains itself


def test_momentum_fades_a_move(base_config, sample_market, healthy_book):
    snap = build_snapshot(base_config, sample_market, Outcome.YES, healthy_book, prior_mid=0.30)
    model = ProbabilityModel(base_config)
    est = model.estimate(sample_market, Outcome.YES, snap, healthy_book, prior_mid=0.30)
    # market jumped 0.30 -> 0.45; estimate should sit below market (faded up-move)
    assert est.estimated_probability < est.market_probability


def test_thresholds_gate(base_config):
    from polybot.models import ProbabilityEstimate
    weak = ProbabilityEstimate(0.50, 0.49, 0.01, 0.9, [])
    strong = ProbabilityEstimate(0.60, 0.50, 0.10, 0.8, [])
    assert not passes_thresholds(base_config, weak)
    assert passes_thresholds(base_config, strong)
