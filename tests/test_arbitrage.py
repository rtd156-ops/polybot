from __future__ import annotations

from polybot.arbitrage import detect_arbitrage
from polybot.models import BookLevel, OrderBook


def _book(ask_price, ask_size=1000, bid_price=None):
    bid_price = bid_price if bid_price is not None else ask_price - 0.05
    return OrderBook(
        token_id="t",
        bids=[BookLevel(bid_price, ask_size)],
        asks=[BookLevel(ask_price, ask_size)],
    )


def test_detects_risk_free_arbitrage(base_config, sample_market):
    # YES ask 0.45 + NO ask 0.50 = 0.95 -> 5c gross edge, well above 1c margin.
    yes_book = _book(0.45)
    no_book = _book(0.50)
    arb = detect_arbitrage(base_config, sample_market, yes_book, no_book)
    assert arb is not None
    assert abs(arb.combined_cost - 0.95) < 1e-9
    assert arb.net_edge >= 0.01
    assert arb.notional_usd > 0


def test_no_arbitrage_when_sum_at_or_above_one(base_config, sample_market):
    arb = detect_arbitrage(base_config, sample_market, _book(0.55), _book(0.50))
    assert arb is None  # 1.05 combined -> no edge


def test_no_arbitrage_below_margin(base_config, sample_market):
    # 0.498 + 0.499 = 0.997 -> 0.3c edge < 1c margin
    arb = detect_arbitrage(base_config, sample_market, _book(0.498), _book(0.499))
    assert arb is None


def test_fee_erodes_thin_edge(base_config, sample_market):
    base_config.data["paper"]["fee_bps"] = 200  # 2% fee kills a thin edge
    base_config.data["arbitrage"]["min_edge"] = 0.005
    # combined 0.98 -> 2c gross, but per-pair fee ~ 0.02*(0.49+0.49) eats it
    arb = detect_arbitrage(base_config, sample_market, _book(0.49), _book(0.49))
    assert arb is None


def test_handles_missing_book(base_config, sample_market):
    assert detect_arbitrage(base_config, sample_market, _book(0.45), None) is None
