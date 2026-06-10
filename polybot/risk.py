"""Risk manager.

Turns an Opportunity into an accepted/rejected Signal. Every rejection carries a
machine-readable reason that the engine persists to ``risk_rejections``.

State it needs (current exposure, today's counters, realized PnL) is supplied by
the caller via RiskState so this module stays pure and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .logging_setup import get_logger
from .models import Opportunity, OrderSide, Outcome, Signal

log = get_logger("polybot.risk")


@dataclass
class RiskState:
    """Snapshot of current portfolio state for risk checks."""

    total_exposure_usd: float = 0.0
    exposure_by_category: dict[str, float] = field(default_factory=dict)
    exposure_by_market: dict[str, float] = field(default_factory=dict)
    new_positions_today: int = 0
    realized_pnl_today: float = 0.0


def _days_to(end_date: Optional[str]) -> Optional[float]:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    return (dt - datetime.now(tz=timezone.utc)).total_seconds() / 86400.0


class RiskManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.r = cfg.section("risk")

    def evaluate(self, opp: Opportunity, state: RiskState) -> Signal:
        """Return a Signal (accepted or rejected). Never raises on policy."""
        market = opp.market
        category = market.category or "Uncategorized"
        notional = float(
            self.cfg.get("paper", "default_order_notional_usd", default=25)
        )

        reject = lambda reason: Signal(  # noqa: E731 - terse local helper
            opportunity=opp, accepted=False, reason=reason
        )

        # --- Daily circuit breakers -----------------------------------------
        if state.realized_pnl_today <= -abs(float(self.r.get("max_daily_loss_usd", 1e9))):
            return reject(f"daily_loss_limit_hit({state.realized_pnl_today:.2f})")
        if state.new_positions_today >= int(self.r.get("max_new_positions_per_day", 1e9)):
            return reject("max_new_positions_per_day")

        # --- Market quality gates (defense in depth vs. scanner) ------------
        if opp.pricing.spread is None or opp.pricing.spread > float(
            self.r.get("max_spread", 0.05)
        ):
            return reject(f"spread_too_wide({opp.pricing.spread})")
        if market.liquidity_usd < float(self.r.get("min_liquidity_usd", 0)):
            return reject(f"liquidity_too_low({market.liquidity_usd:.0f})")

        days = _days_to(market.end_date)
        if days is not None:
            if days < float(self.r.get("min_days_to_resolution", 0)):
                return reject(f"resolves_too_soon({days:.1f}d)")
            if days > float(self.r.get("max_days_to_resolution", 1e9)):
                return reject(f"resolves_too_far({days:.0f}d)")

        # --- Exposure limits -------------------------------------------------
        mkt_exp = state.exposure_by_market.get(market.market_id, 0.0)
        if mkt_exp + notional > float(self.r.get("max_notional_per_market_usd", 1e9)):
            return reject(f"per_market_limit({mkt_exp:.0f}+{notional:.0f})")

        cat_exp = state.exposure_by_category.get(category, 0.0)
        if cat_exp + notional > float(self.r.get("max_notional_per_category_usd", 1e9)):
            return reject(f"per_category_limit({category}:{cat_exp:.0f}+{notional:.0f})")

        if state.total_exposure_usd + notional > float(
            self.r.get("max_total_exposure_usd", 1e9)
        ):
            return reject(f"total_exposure_limit({state.total_exposure_usd:.0f})")

        # --- Accepted: derive a limit order intent --------------------------
        side = OrderSide.BUY  # V1 only opens long YES/NO positions
        target = opp.pricing.best_ask if opp.outcome is Outcome.YES else opp.pricing.best_ask
        token = opp.pricing.token_id
        return Signal(
            opportunity=opp,
            accepted=True,
            reason="accepted",
            side=side,
            token_id=token,
            target_price=target,
            notional_usd=notional,
        )
