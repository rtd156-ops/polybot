"""Domain models -- plain dataclasses shared across the pipeline.

Kept dependency-free and serialization-friendly (``to_dict``) so they can be
logged, persisted to SQLite, and shipped in webhook payloads uniformly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"


@dataclass
class Market:
    """Normalized Polymarket market (binary YES/NO)."""

    market_id: str                     # Gamma market id
    condition_id: str                  # on-chain condition id
    question: str
    slug: str
    category: Optional[str]
    active: bool
    closed: bool
    volume_usd: float
    liquidity_usd: float
    end_date: Optional[str]            # ISO8601
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    yes_price: Optional[float]         # Gamma-reported outcome price (0-1)
    no_price: Optional[float]
    url: str
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class BookLevel:
    price: float
    size: float                        # number of shares


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel]              # sorted best (highest price) first
    asks: list[BookLevel]             # sorted best (lowest price) first
    fetched_at: str = field(default_factory=utcnow)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def notional_within(self, side: str, levels: int = 3) -> float:
        """USD notional resting on a side across the top ``levels`` (price*size)."""
        book = self.asks if side == "ask" else self.bids
        return sum(lvl.price * lvl.size for lvl in book[:levels])


@dataclass
class BookFill:
    """Result of walking an order book to fill a target notional."""

    avg_price: float                       # size-weighted average fill price
    filled_size: float                     # shares actually filled
    filled_notional: float                 # USD spent (price*size summed)
    levels_consumed: int                   # how many book levels were touched
    fully_filled: bool                     # False if book ran out of liquidity


@dataclass
class PricingSnapshot:
    """Computed pricing view of a market outcome at a point in time."""

    market_id: str
    token_id: str
    outcome: Outcome
    best_bid: Optional[float]
    best_ask: Optional[float]
    mid: Optional[float]
    spread: Optional[float]
    implied_probability: Optional[float]   # = mid for YES token
    useful_liquidity_usd: float            # min(entry, exit) notional available
    signals: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProbabilityEstimate:
    estimated_probability: float
    market_probability: float
    edge: float                            # estimated - market (signed)
    confidence: float                      # 0..1
    reasons: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Opportunity:
    market: Market
    outcome: Outcome
    pricing: PricingSnapshot
    estimate: ProbabilityEstimate
    suggested_action: str                  # e.g. "BUY YES", "BUY NO", "WATCH"
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        return {
            "market_id": self.market.market_id,
            "question": self.market.question,
            "category": self.market.category,
            "url": self.market.url,
            "outcome": self.outcome.value,
            "market_price": self.pricing.mid,
            "spread": self.pricing.spread,
            "useful_liquidity_usd": self.pricing.useful_liquidity_usd,
            "estimated_probability": self.estimate.estimated_probability,
            "market_probability": self.estimate.market_probability,
            "edge": self.estimate.edge,
            "confidence": self.estimate.confidence,
            "reasons": self.estimate.reasons,
            "pricing_signals": self.pricing.signals,
            "suggested_action": self.suggested_action,
            "created_at": self.created_at,
        }


@dataclass
class Signal:
    """An opportunity that passed (or failed) risk -> a trade intent."""

    opportunity: Opportunity
    accepted: bool
    reason: str                            # acceptance note or rejection reason
    side: Optional[OrderSide] = None
    token_id: Optional[str] = None
    target_price: Optional[float] = None
    notional_usd: Optional[float] = None
    created_at: str = field(default_factory=utcnow)


@dataclass
class OrderResult:
    """Uniform result returned by any ExecutionClient (paper or live)."""

    order_id: str
    status: OrderStatus
    market_id: str
    token_id: str
    outcome: Outcome
    side: OrderSide
    price: float
    size: float                            # shares
    filled_size: float
    notional_usd: float
    is_paper: bool
    reason: str = ""
    fee_usd: float = 0.0                    # modeled exchange fee
    slippage_bps: float = 0.0              # avg fill vs mid, in basis points
    created_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["outcome"] = self.outcome.value
        d["side"] = self.side.value
        return d
