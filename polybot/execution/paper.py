"""Paper execution engine.

Simulates LIMIT orders with a *conservative* fill model: you pay the far side of
the book (the ask when buying), never the mid, plus optional extra slippage.
This biases paper results pessimistically so they don't flatter the strategy.

Positions, blocked capital and PnL live in the ledger; this class only produces
OrderResults and fill prices. Closing is modeled by selling into the bid.
"""

from __future__ import annotations

import uuid
from typing import Optional

from ..config import Config
from ..logging_setup import get_logger
from ..models import OrderBook, OrderResult, OrderSide, OrderStatus, Outcome
from .base import ExecutionClient

log = get_logger("polybot.paper")


class PaperExecution(ExecutionClient):
    is_paper = True

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.slippage = float(cfg.get("paper", "taker_slippage", default=0.0))
        self.fill_against_book = bool(
            cfg.get("paper", "fill_against_book", default=True)
        )
        self._orders: dict[str, OrderResult] = {}

    def place_limit_order(
        self,
        *,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        side: OrderSide,
        price: float,
        notional_usd: float,
        reference_book: Optional[OrderBook] = None,
    ) -> OrderResult:
        fill_price = self._conservative_fill_price(side, price, reference_book)
        if fill_price <= 0 or fill_price >= 1:
            return self._rejected(
                market_id, token_id, outcome, side, price, notional_usd,
                reason=f"invalid_fill_price({fill_price})",
            )

        size = round(notional_usd / fill_price, 4)
        order = OrderResult(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            status=OrderStatus.FILLED,  # conservative model: immediate full fill
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            price=round(fill_price, 4),
            size=size,
            filled_size=size,
            notional_usd=round(fill_price * size, 4),
            is_paper=True,
            reason="paper_fill",
        )
        self._orders[order.order_id] = order
        log.info(
            "PAPER %s %s %s @ %.4f size=%.2f notional=$%.2f",
            side.value, outcome.value, market_id, fill_price, size, order.notional_usd,
        )
        return order

    def _conservative_fill_price(
        self, side: OrderSide, limit_price: float, book: Optional[OrderBook]
    ) -> float:
        """Buy: pay the ask (+slip). Sell: hit the bid (-slip). Fallback: limit."""
        if not (self.fill_against_book and book):
            return limit_price
        if side is OrderSide.BUY:
            ref = book.best_ask if book.best_ask is not None else limit_price
            return ref + self.slippage
        ref = book.best_bid if book.best_bid is not None else limit_price
        return ref - self.slippage

    def simulate_close(
        self, position_outcome: Outcome, size: float, reference_book: Optional[OrderBook],
        fallback_price: float,
    ) -> float:
        """Return the conservative price at which we could close (sell into bid)."""
        if self.fill_against_book and reference_book and reference_book.best_bid is not None:
            return max(0.0, reference_book.best_bid - self.slippage)
        return fallback_price

    def cancel_order(self, order_id: str) -> OrderResult:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"unknown paper order {order_id}")
        order.status = OrderStatus.CANCELLED
        return order

    def get_order_status(self, order_id: str) -> Optional[OrderResult]:
        return self._orders.get(order_id)

    def _rejected(self, market_id, token_id, outcome, side, price, notional, reason) -> OrderResult:
        log.warning("PAPER order rejected: %s", reason)
        return OrderResult(
            order_id=f"paper-rej-{uuid.uuid4().hex[:8]}",
            status=OrderStatus.REJECTED,
            market_id=market_id, token_id=token_id, outcome=outcome, side=side,
            price=price, size=0.0, filled_size=0.0, notional_usd=0.0,
            is_paper=True, reason=reason,
        )
