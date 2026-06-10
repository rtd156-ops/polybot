"""Market scanner: fetch -> filter -> normalize active Polymarket markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .logging_setup import get_logger
from .models import Market

log = get_logger("polybot.scanner")


@dataclass
class FilterResult:
    market: Market
    passed: bool
    reason: str = ""


def _days_to(end_date: Optional[str]) -> Optional[float]:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    delta = dt - datetime.now(tz=timezone.utc)
    return delta.total_seconds() / 86400.0


def looks_ambiguous(market: Market, keywords: list[str]) -> bool:
    text = f"{market.question} {market.slug}".lower()
    return any(kw.lower() in text for kw in keywords)


def filter_market(market: Market, cfg: Config) -> FilterResult:
    """Apply scanner filters to one market. First failing rule wins."""
    sc = cfg.section("scanner")

    if sc.get("require_active", True) and not market.active:
        return FilterResult(market, False, "inactive")
    if sc.get("require_open", True) and market.closed:
        return FilterResult(market, False, "closed")
    if market.volume_usd < float(sc.get("min_volume_usd", 0)):
        return FilterResult(market, False, f"low_volume({market.volume_usd:.0f})")
    if market.liquidity_usd < float(sc.get("min_liquidity_usd", 0)):
        return FilterResult(market, False, f"low_liquidity({market.liquidity_usd:.0f})")

    allowed = sc.get("allowed_categories") or []
    if allowed:
        if market.category is None:
            # Gamma's /markets often omits category (it lives on the event).
            # Only block uncategorized markets when explicitly told to.
            if not sc.get("allow_uncategorized", True):
                return FilterResult(market, False, "category_missing")
        elif market.category not in allowed:
            return FilterResult(market, False, f"category_not_allowed({market.category})")

    days = _days_to(market.end_date)
    if days is not None:
        if days < float(sc.get("min_days_to_resolution", 0)):
            return FilterResult(market, False, f"resolves_too_soon({days:.1f}d)")
        if days > float(sc.get("max_days_to_resolution", 10**9)):
            return FilterResult(market, False, f"resolves_too_far({days:.0f}d)")

    if looks_ambiguous(market, sc.get("ambiguous_keywords") or []):
        return FilterResult(market, False, "ambiguous_resolution")

    if not (market.yes_token_id and market.no_token_id):
        return FilterResult(market, False, "missing_clob_tokens")

    return FilterResult(market, True, "ok")


class MarketScanner:
    def __init__(self, cfg: Config, gamma_client) -> None:
        self.cfg = cfg
        self.gamma = gamma_client

    def scan(self) -> tuple[list[Market], list[FilterResult]]:
        """Returns (passing_markets, all_filter_results)."""
        limit = int(self.cfg.get("scanner", "max_markets_scanned", default=400))
        raw = self.gamma.fetch_markets(limit=limit)
        results = [filter_market(m, self.cfg) for m in raw]
        passing = [r.market for r in results if r.passed]
        log.info(
            "Scanned %d markets, %d passed filters", len(raw), len(passing)
        )
        return passing, results
