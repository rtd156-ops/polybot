"""Gamma API client -- PUBLIC market/event metadata. No authentication.

Docs: https://docs.polymarket.com (Gamma Markets API). We tolerate schema drift
by parsing defensively and keeping the raw payload on the Market for auditing.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from ..logging_setup import get_logger
from ..models import Market
from .http import HttpClient

log = get_logger("polybot.gamma")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_jsonish_list(value: Any) -> list:
    """Gamma returns some array fields as JSON-encoded strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


class GammaClient:
    def __init__(self, base_url: str, http: HttpClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http

    def fetch_markets(self, limit: int = 400, extra_params: Optional[dict] = None) -> list[Market]:
        """Fetch active, open markets and normalize them.

        Paginates in chunks of 100 (Gamma's typical max) up to ``limit``.
        """
        markets: list[Market] = []
        page_size = 100
        offset = 0
        while len(markets) < limit:
            params = {
                "active": "true",
                "closed": "false",
                "limit": str(min(page_size, limit - len(markets))),
                "offset": str(offset),
                "order": "volume",
                "ascending": "false",
            }
            if extra_params:
                params.update(extra_params)
            try:
                payload = self.http.get_json(f"{self.base_url}/markets", params=params)
            except Exception as exc:
                log.error("Gamma fetch failed at offset %d: %s", offset, exc)
                break

            rows = self._extract_rows(payload)
            if not rows:
                break
            for row in rows:
                m = self.normalize_market(row)
                if m is not None:
                    markets.append(m)
            offset += len(rows)
            if len(rows) < page_size:
                break
        log.info("Gamma returned %d normalized markets", len(markets))
        return markets

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            for key in ("data", "markets", "results"):
                if isinstance(payload.get(key), list):
                    return [r for r in payload[key] if isinstance(r, dict)]
        return []

    @staticmethod
    def normalize_market(row: dict) -> Optional[Market]:
        """Map a raw Gamma row to our Market. Returns None if unusable."""
        market_id = str(row.get("id") or row.get("marketId") or "").strip()
        condition_id = str(row.get("conditionId") or "").strip()
        if not market_id:
            return None

        outcomes = [str(o).upper() for o in _parse_jsonish_list(row.get("outcomes"))]
        prices = [_to_float(p) for p in _parse_jsonish_list(row.get("outcomePrices"))]
        token_ids = [str(t) for t in _parse_jsonish_list(row.get("clobTokenIds"))]

        yes_idx, no_idx = GammaClient._yes_no_indices(outcomes)
        yes_price = prices[yes_idx] if yes_idx is not None and yes_idx < len(prices) else None
        no_price = prices[no_idx] if no_idx is not None and no_idx < len(prices) else None
        yes_token = token_ids[yes_idx] if yes_idx is not None and yes_idx < len(token_ids) else None
        no_token = token_ids[no_idx] if no_idx is not None and no_idx < len(token_ids) else None

        slug = str(row.get("slug") or "")
        url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"

        return Market(
            market_id=market_id,
            condition_id=condition_id,
            question=str(row.get("question") or row.get("title") or "").strip(),
            slug=slug,
            category=(str(row["category"]).strip() if row.get("category") else None),
            active=bool(row.get("active", True)),
            closed=bool(row.get("closed", False)),
            volume_usd=_to_float(row.get("volume") or row.get("volumeNum")) or 0.0,
            liquidity_usd=_to_float(row.get("liquidity") or row.get("liquidityNum")) or 0.0,
            end_date=(str(row["endDate"]) if row.get("endDate") else None),
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_price=yes_price,
            no_price=no_price,
            url=url,
            raw=row,
        )

    @staticmethod
    def _yes_no_indices(outcomes: Iterable[str]) -> tuple[Optional[int], Optional[int]]:
        outcomes = list(outcomes)
        yes_idx = no_idx = None
        for i, o in enumerate(outcomes):
            if o == "YES":
                yes_idx = i
            elif o == "NO":
                no_idx = i
        # Fallback for unlabeled binary markets: assume [YES, NO] ordering.
        if yes_idx is None and no_idx is None and len(outcomes) == 2:
            return 0, 1
        return yes_idx, no_idx
