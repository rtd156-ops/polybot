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
    bankroll_usd: float = 0.0              # current equity used for Kelly sizing
    peak_equity_usd: float = 0.0           # high-water mark for drawdown gate
    current_equity_usd: float = 0.0


def kelly_fraction(estimated_prob: float, entry_price: float) -> float:
    """Fractional Kelly stake for buying a binary share at ``entry_price``.

    Buying at price p to win 1 has net odds b=(1-p)/p. With win prob q the
    full-Kelly fraction simplifies to (q - p)/(1 - p). Returns 0 if there's no
    positive edge (never sizes a negative-EV bet).
    """
    p = entry_price
    q = estimated_prob
    if not (0 < p < 1) or not (0 < q < 1):
        return 0.0
    f = (q - p) / (1.0 - p)
    return max(0.0, f)


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

        # Entry price = what a taker pays now (best ask of the chosen outcome).
        entry_price = opp.pricing.best_ask or opp.pricing.mid or 0.5

        reject = lambda reason: Signal(  # noqa: E731 - terse local helper
            opportunity=opp, accepted=False, reason=reason
        )

        # --- Daily circuit breakers -----------------------------------------
        if state.realized_pnl_today <= -abs(float(self.r.get("max_daily_loss_usd", 1e9))):
            return reject(f"daily_loss_limit_hit({state.realized_pnl_today:.2f})")
        if state.new_positions_today >= int(self.r.get("max_new_positions_per_day", 1e9)):
            return reject("max_new_positions_per_day")

        # --- Max-drawdown circuit breaker (peak-to-current equity) ----------
        max_dd = float(self.r.get("max_drawdown_pct", 0) or 0)
        if max_dd > 0 and state.peak_equity_usd > 0:
            dd = (state.peak_equity_usd - state.current_equity_usd) / state.peak_equity_usd
            if dd >= max_dd:
                return reject(f"max_drawdown_hit({dd*100:.1f}%>={max_dd*100:.0f}%)")

        # --- Price floor/ceiling: extremes are rarely tradeable edge --------
        min_p = float(self.r.get("min_price", 0) or 0)
        max_p = float(self.r.get("max_price", 1) or 1)
        if entry_price < min_p or entry_price > max_p:
            return reject(f"price_out_of_band({entry_price:.3f})")

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

        # --- Position sizing (Kelly or fixed) -------------------------------
        notional = self._size_position(opp, state, entry_price)
        if notional <= 0:
            return reject("kelly_zero_or_negative_edge")

        # --- Exposure limits (cap notional down to fit, then re-check) ------
        mkt_cap = float(self.r.get("max_notional_per_market_usd", 1e9))
        cat_cap = float(self.r.get("max_notional_per_category_usd", 1e9))
        tot_cap = float(self.r.get("max_total_exposure_usd", 1e9))
        mkt_exp = state.exposure_by_market.get(market.market_id, 0.0)
        cat_exp = state.exposure_by_category.get(category, 0.0)

        # Allow Kelly to be trimmed to the remaining headroom rather than
        # rejected outright -- but only down to a sane minimum ticket.
        headroom = min(mkt_cap - mkt_exp, cat_cap - cat_exp, tot_cap - state.total_exposure_usd)
        notional = min(notional, headroom)
        min_ticket = float(self.r.get("min_ticket_usd", 5))
        if notional < min_ticket:
            if mkt_exp + min_ticket > mkt_cap:
                return reject(f"per_market_limit({mkt_exp:.0f}/{mkt_cap:.0f})")
            if cat_exp + min_ticket > cat_cap:
                return reject(f"per_category_limit({category}:{cat_exp:.0f}/{cat_cap:.0f})")
            if state.total_exposure_usd + min_ticket > tot_cap:
                return reject(f"total_exposure_limit({state.total_exposure_usd:.0f}/{tot_cap:.0f})")
            return reject(f"below_min_ticket({notional:.2f})")

        # --- Accepted: derive a limit order intent --------------------------
        side = OrderSide.BUY  # V1 only opens long YES/NO positions
        token = opp.pricing.token_id
        return Signal(
            opportunity=opp,
            accepted=True,
            reason="accepted",
            side=side,
            token_id=token,
            target_price=entry_price,
            notional_usd=round(notional, 2),
        )

    def _size_position(self, opp: Opportunity, state: RiskState, entry_price: float) -> float:
        """Return the target stake in USD: fractional Kelly or a fixed ticket."""
        if not bool(self.r.get("use_kelly", False)):
            return float(self.cfg.get("paper", "default_order_notional_usd", default=25))

        bankroll = state.bankroll_usd or float(
            self.cfg.get("paper", "starting_balance_usd", default=1000)
        )
        mult = float(self.r.get("kelly_multiplier", 0.5))   # half-Kelly default
        cap = float(self.r.get("max_kelly_fraction", 0.1))  # never bet > cap of bankroll
        f = kelly_fraction(opp.estimate.estimated_probability, entry_price) * mult
        f = min(f, cap)
        return bankroll * f
