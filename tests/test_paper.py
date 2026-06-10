from __future__ import annotations

from polybot.execution.paper import PaperExecution
from polybot.models import OrderSide, OrderStatus, Outcome


def test_paper_fills_against_ask_not_mid(base_config, healthy_book):
    pe = PaperExecution(base_config)
    order = pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=0.45, notional_usd=25.0, reference_book=healthy_book,
    )
    # conservative: pays the ask (0.46), not the mid (0.45)
    assert order.status is OrderStatus.FILLED
    assert order.price == 0.46
    assert order.size == round(25.0 / 0.46, 4)


def test_paper_slippage_applied(base_config, healthy_book):
    base_config.data["paper"]["taker_slippage"] = 0.01
    pe = PaperExecution(base_config)
    order = pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=0.45, notional_usd=25.0, reference_book=healthy_book,
    )
    assert order.price == 0.47  # 0.46 ask + 0.01 slippage


def test_paper_close_sells_into_bid(base_config, healthy_book):
    pe = PaperExecution(base_config)
    exit_price = pe.simulate_close(Outcome.YES, 50.0, healthy_book, fallback_price=0.45)
    assert exit_price == 0.44  # best bid


def test_paper_cancel(base_config, healthy_book):
    pe = PaperExecution(base_config)
    order = pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=0.45, notional_usd=25.0, reference_book=healthy_book,
    )
    cancelled = pe.cancel_order(order.order_id)
    assert cancelled.status is OrderStatus.CANCELLED


def test_paper_rejects_degenerate_price(base_config):
    pe = PaperExecution(base_config)
    order = pe.place_limit_order(
        market_id="123", token_id="tok-yes", outcome=Outcome.YES,
        side=OrderSide.BUY, price=1.5, notional_usd=25.0, reference_book=None,
    )
    assert order.status is OrderStatus.REJECTED
