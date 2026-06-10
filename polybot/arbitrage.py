"""Risk-free arbitrage detection on a single binary market.

For a binary market, YES + NO settle to exactly $1. If you can buy a YES share
and a NO share for a combined cost below $1 (minus fees and a safety margin),
you lock in a profit regardless of resolution. This module only *detects* the
opportunity; sizing/execution is left to the engine + risk manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .models import Market, OrderBook
from .pricing import exchange_fee_usd, walk_book


@dataclass
class ArbOpportunity:
    market_id: str
    question: str
    url: str
    yes_ask: float
    no_ask: float
    combined_cost: float          # yes_ask + no_ask (per pair)
    gross_edge: float             # 1 - combined_cost (per $1 pair, pre-fee)
    net_edge: float               # after modeled fees
    max_pairs: float              # pairs fillable given book depth
    notional_usd: float           # USD to deploy for max_pairs

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def detect_arbitrage(
    cfg: Config,
    market: Market,
    yes_book: Optional[OrderBook],
    no_book: Optional[OrderBook],
) -> Optional[ArbOpportunity]:
    """Return an ArbOpportunity if YES_ask + NO_ask < 1 - margin (net of fees)."""
    if yes_book is None or no_book is None:
        return None
    ya, na = yes_book.best_ask, no_book.best_ask
    if ya is None or na is None:
        return None

    combined = ya + na
    gross_edge = 1.0 - combined
    margin = float(cfg.get("arbitrage", "min_edge", default=0.01))
    fee_bps = float(cfg.get("paper", "fee_bps", default=0.0))

    # Per-pair fee = fee on the YES leg + fee on the NO leg (1 share each).
    per_pair_fee = exchange_fee_usd(fee_bps, ya, 1.0) + exchange_fee_usd(fee_bps, na, 1.0)
    net_edge = gross_edge - per_pair_fee
    if net_edge < margin:
        return None

    # Depth: how many pairs can we actually fill at/around these asks?
    budget = float(cfg.get("arbitrage", "max_notional_usd", default=100))
    yes_fill = walk_book(yes_book.asks, budget * (ya / combined))
    no_fill = walk_book(no_book.asks, budget * (na / combined))
    max_pairs = min(yes_fill.filled_size, no_fill.filled_size)
    if max_pairs <= 0:
        return None

    return ArbOpportunity(
        market_id=market.market_id,
        question=market.question,
        url=market.url,
        yes_ask=round(ya, 4),
        no_ask=round(na, 4),
        combined_cost=round(combined, 4),
        gross_edge=round(gross_edge, 4),
        net_edge=round(net_edge, 4),
        max_pairs=round(max_pairs, 2),
        notional_usd=round(max_pairs * combined, 2),
    )
