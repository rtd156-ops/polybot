"""Probability model (heuristic V1).

Deliberately simple, transparent and *extensible*. It blends a handful of
normalized features into an adjustment around the market's implied probability,
then reports edge, confidence and human-readable reasons.

Extending later: implement ``ProbabilityModel.estimate`` in a subclass (ML,
news, or LLM analysis) and swap it in via the engine. The output contract
(ProbabilityEstimate) stays identical, so nothing downstream changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .logging_setup import get_logger
from .models import Market, Outcome, OrderBook, PricingSnapshot, ProbabilityEstimate

log = get_logger("polybot.probability")


# Coarse category priors: how much we trust the market vs. expect noise.
# Higher prior_confidence => we lean on the market price; lower => more skeptical.
_CATEGORY_PRIORS = {
    "Crypto": 0.55,
    "Politics": 0.50,
    "Sports": 0.60,
    "Economy": 0.50,
    "Uncategorized": 0.40,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _days_to_close(end_date: Optional[str]) -> Optional[float]:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    return (dt - datetime.now(tz=timezone.utc)).total_seconds() / 86400.0


class ProbabilityModel:
    """Heuristic estimator. Subclass and override ``estimate`` to extend."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.weights = cfg.get("probability", "weights", default={})

    def estimate(
        self,
        market: Market,
        outcome: Outcome,
        snapshot: PricingSnapshot,
        book: Optional[OrderBook],
        prior_mid: Optional[float] = None,
    ) -> Optional[ProbabilityEstimate]:
        market_prob = snapshot.implied_probability
        if market_prob is None:
            return None

        feats: dict[str, float] = {}
        reasons: list[str] = []

        # --- Feature 1: momentum (recent move, mean-reverting bias) ----------
        # We treat a sharp move as partially over-extended -> nudge back toward
        # the pre-move level. Bounded contribution.
        momentum_adj = 0.0
        if prior_mid is not None:
            move = market_prob - prior_mid
            momentum_adj = -0.25 * move  # fade 25% of the move
            if abs(move) >= 0.05:
                reasons.append(f"recent move {move:+.3f} (faded)")
        feats["momentum_adj"] = momentum_adj

        # --- Feature 2: liquidity quality -> confidence, not direction -------
        liq = market.liquidity_usd
        liq_score = _clamp(liq / 50000.0)  # saturates at $50k
        feats["liquidity_score"] = liq_score

        # --- Feature 3: time to close -> confidence -------------------------
        days = _days_to_close(market.end_date)
        if days is None:
            time_score = 0.4
        else:
            # Sweet spot ~3-45 days; very near/very far => less confidence.
            if days < 1:
                time_score = 0.2
            elif days <= 45:
                time_score = 0.8
            else:
                time_score = _clamp(0.8 - (days - 45) / 200.0, 0.2, 0.8)
        feats["time_score"] = time_score

        # --- Feature 4: orderbook stability -> confidence -------------------
        stability = self._orderbook_stability(book, snapshot)
        feats["stability_score"] = stability

        # --- Feature 5: category prior -> confidence ------------------------
        cat_prior = _CATEGORY_PRIORS.get(market.category or "Uncategorized", 0.4)
        feats["category_prior"] = cat_prior

        # --- Direction: estimate = market + small bounded adjustments -------
        # Extreme-price reversion: extremes are often slightly over-confident.
        extreme_adj = 0.0
        if market_prob >= 0.92:
            extreme_adj = -0.02
            reasons.append("extreme-high price, slight reversion")
        elif market_prob <= 0.08:
            extreme_adj = 0.02
            reasons.append("extreme-low price, slight reversion")
        feats["extreme_adj"] = extreme_adj

        estimated = _clamp(market_prob + momentum_adj + extreme_adj, 0.01, 0.99)

        # --- Confidence: weighted blend of quality scores -------------------
        w = self.weights
        confidence = (
            float(w.get("momentum", 0.3)) * (1.0 - min(abs(momentum_adj) * 4, 1.0))
            + float(w.get("liquidity", 0.2)) * liq_score
            + float(w.get("time_to_close", 0.15)) * time_score
            + float(w.get("orderbook_stability", 0.2)) * stability
            + float(w.get("category_prior", 0.15)) * cat_prior
        )
        confidence = _clamp(confidence)

        edge = estimated - market_prob
        if not reasons:
            reasons.append("no strong edge; market-aligned")
        reasons.append(
            f"liq={liq_score:.2f} time={time_score:.2f} stab={stability:.2f}"
        )

        return ProbabilityEstimate(
            estimated_probability=round(estimated, 4),
            market_probability=round(market_prob, 4),
            edge=round(edge, 4),
            confidence=round(confidence, 4),
            reasons=reasons,
            features=feats,
        )

    @staticmethod
    def _orderbook_stability(book: Optional[OrderBook], snap: PricingSnapshot) -> float:
        """0..1: tight spread + balanced depth => stable book."""
        if book is None or snap.spread is None:
            return 0.3
        spread_score = _clamp(1.0 - snap.spread / 0.10)  # 0 spread=>1, .10=>0
        bid_depth = book.notional_within("bid", 3)
        ask_depth = book.notional_within("ask", 3)
        if bid_depth + ask_depth <= 0:
            balance = 0.0
        else:
            balance = 1.0 - abs(bid_depth - ask_depth) / (bid_depth + ask_depth)
        return _clamp(0.6 * spread_score + 0.4 * balance)


def passes_thresholds(cfg: Config, est: ProbabilityEstimate) -> bool:
    return (
        abs(est.edge) >= float(cfg.get("probability", "min_edge", default=0.04))
        and est.confidence >= float(cfg.get("probability", "min_confidence", default=0.35))
    )
