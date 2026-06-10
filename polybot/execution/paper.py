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
from ..pricing import exchange_fee_usd, slippage_bps, walk_book

log = get_logger("polybot.paper")


class PaperExecution(ExecutionClient):
    is_paper = True

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.slippage = float(cfg.get("paper", "taker_slippage", default=0.0))
        self.fill_against_book = bool(
            cfg.get("paper", "fill_against_book", default=True)
        )
        self.fee_bps = float(cfg.get("paper", "fee_bps", default=0.0))
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
        # Walk the real book level-by-level (taker model). Buying eats the asks,
        # selling hits the bids. Falls back to the limit price if no book.
        mid = reference_book.mid if reference_book else None
        if self.fill_against_book and reference_book is not None:
            levels = reference_book.asks if side is OrderSide.BUY else reference_book.bids
            fill = walk_book(levels, notional_usd)
            if fill.filled_size <= 0:
                return self._rejected(
                    market_id, token_id, outcome, side, price, notional_usd,
                    reason="no_book_liquidity",
                )
            fill_price = fill.avg_price + (self.slippage if side is OrderSide.BUY else -self.slippage)
            size = round(fill.filled_size, 6)
            status = OrderStatus.FILLED if fill.fully_filled else OrderStatus.PARTIAL
        else:
            if price <= 0 or price >= 1:
                return self._rejected(
                    market_id, token_id, outcome, side, price, notional_usd,
                    reason=f"invalid_fill_price({price})",
                )
            fill_price = price
            size = round(notional_usd / price, 6)
            status = OrderStatus.FILLED

        if fill_price <= 0 or fill_price >= 1:
            return self._rejected(
                market_id, token_id, outcome, side, price, notional_usd,
                reason=f"invalid_fill_price({fill_price:.4f})",
            )

        fee = exchange_fee_usd(self.fee_bps, fill_price, size)
        order = OrderResult(
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            status=status,
            market_id=market_id,
            token_id=token_id,
            outcome=outcome,
            side=side,
            price=round(fill_price, 4),
            size=size,
            filled_size=size,
            notional_usd=round(fill_price * size, 4),
            is_paper=True,
            reason="paper_fill" if status is OrderStatus.FILLED else "paper_partial_fill",
            fee_usd=round(fee, 6),
            slippage_bps=round(slippage_bps(fill_price, mid), 2),
        )
        self._orders[order.order_id] = order
        log.info(
            "PAPER %s %s %s @ %.4f size=%.2f notional=$%.2f fee=$%.4f slip=%.1fbps (%s)",
            side.value, outcome.value, market_id, fill_price, size,
            order.notional_usd, fee, order.slippage_bps, status.value,
        )
        return order

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
