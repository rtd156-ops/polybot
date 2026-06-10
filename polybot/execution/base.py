"""Common execution interface shared by paper and live engines.

The engine codes against ``ExecutionClient`` only, so swapping paper -> live is a
construction-time decision, not a rewrite. Both implementations return the same
``OrderResult`` shape.
"""

from __future__ import annotations

import abc
from typing import Optional

from ..models import OrderResult, OrderSide, Outcome


class ExecutionClient(abc.ABC):
    """Abstract execution client. Implementations: PaperExecution, live."""

    #: True for simulated engines. Used by the engine for routing/labelling.
    is_paper: bool = True

    @abc.abstractmethod
    def place_limit_order(
        self,
        *,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        side: OrderSide,
        price: float,
        notional_usd: float,
        reference_book=None,
    ) -> OrderResult:
        """Place (or simulate) a LIMIT order. Never market-aggressive."""

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        ...

    @abc.abstractmethod
    def get_order_status(self, order_id: str) -> Optional[OrderResult]:
        ...

    def close(self) -> None:  # optional cleanup hook
        ...
