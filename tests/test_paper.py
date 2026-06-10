from __future__ import annotations

from polybot.execution.paper import PaperExecution
from polybot.models import BookLevel, OrderBook, OrderSide, OrderStatus, Outcome


def _buy(pe, book, notional=25.0):
    return pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=0.45, notional_usd=notional, reference_book=book,
    )


def test_paper_fills_walking_top_of_book(base_config, healthy_book):
    pe = PaperExecution(base_config)
    order = _buy(pe, healthy_book)
    # $25 fits entirely in the best ask level (0.46 x 2000) -> avg 0.46.
    assert order.status is OrderStatus.FILLED
    assert order.price == 0.46
    assert abs(order.size - 25.0 / 0.46) < 1e-3


def test_paper_walks_multiple_levels(base_config):
    # Thin top level forces the fill to consume deeper, worse-priced levels.
    book = OrderBook(
        token_id="t",
        bids=[BookLevel(0.40, 1000)],
        asks=[BookLevel(0.50, 10), BookLevel(0.60, 1000)],  # 0.50*10=$5 only
    )
    pe = PaperExecution(base_config)
    order = _buy(pe, book, notional=25.0)
    # $5 at 0.50 then $20 at 0.60 -> avg between 0.50 and 0.60, worse than top.
    assert 0.50 < order.price < 0.60
    assert order.status is OrderStatus.FILLED


def test_paper_partial_when_book_too_thin(base_config):
    book = OrderBook(token_id="t", bids=[BookLevel(0.40, 10)],
                     asks=[BookLevel(0.50, 10)])  # only $5 of depth
    pe = PaperExecution(base_config)
    order = _buy(pe, book, notional=25.0)
    assert order.status is OrderStatus.PARTIAL
    assert order.filled_size == 10  # consumed all available shares


def test_paper_slippage_applied(base_config, healthy_book):
    base_config.data["paper"]["taker_slippage"] = 0.01
    pe = PaperExecution(base_config)
    order = _buy(pe, healthy_book)
    assert order.price == 0.47  # 0.46 avg + 0.01 slippage


def test_paper_fee_modeled(base_config, healthy_book):
    base_config.data["paper"]["fee_bps"] = 100  # 1%
    pe = PaperExecution(base_config)
    order = _buy(pe, healthy_book)
    # fee = 0.01 * min(0.46, 0.54) * size
    assert order.fee_usd > 0
    assert abs(order.fee_usd - 0.01 * 0.46 * order.size) < 1e-6


def test_paper_slippage_bps_reported(base_config, healthy_book):
    pe = PaperExecution(base_config)
    order = _buy(pe, healthy_book)
    # fill 0.46 vs mid 0.45 -> ~22 bps
    assert order.slippage_bps > 0


def test_paper_close_sells_into_bid(base_config, healthy_book):
    pe = PaperExecution(base_config)
    exit_price = pe.simulate_close(Outcome.YES, 50.0, healthy_book, fallback_price=0.45)
    assert exit_price == 0.44  # best bid


def test_paper_cancel(base_config, healthy_book):
    pe = PaperExecution(base_config)
    order = _buy(pe, healthy_book)
    cancelled = pe.cancel_order(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED


def test_paper_rejects_without_book_and_bad_price(base_config):
    pe = PaperExecution(base_config)
    order = pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=1.5, notional_usd=25.0, reference_book=None,
    )
    assert order.status is OrderStatus.REJECTED
