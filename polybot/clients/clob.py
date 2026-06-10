"""CLOB API client -- PUBLIC read paths only (order book / prices / midpoint).

No authentication and no trading here. Order placement lives behind the gated
live execution client. Docs: https://docs.polymarket.com (CLOB API).
"""

from __future__ import annotations

from typing import Any, Optional

from ..logging_setup import get_logger
from ..models import BookLevel, OrderBook
from .http import HttpClient

log = get_logger("polybot.clob")


class ClobDataClient:
    def __init__(self, base_url: str, http: HttpClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http

    def fetch_book(self, token_id: str) -> Optional[OrderBook]:
        """GET /book?token_id=... -> normalized OrderBook (sorted)."""
        if not token_id:
            return None
        try:
            payload = self.http.get_json(
                f"{self.base_url}/book", params={"token_id": token_id}
            )
        except Exception as exc:
            log.warning("CLOB book fetch failed for %s: %s", token_id[:10], exc)
            return None
        return self._parse_book(token_id, payload)

    @staticmethod
    def _parse_book(token_id: str, payload: Any) -> Optional[OrderBook]:
        if not isinstance(payload, dict):
            return None

        def levels(key: str) -> list[BookLevel]:
            out: list[BookLevel] = []
            for entry in payload.get(key, []) or []:
                try:
                    price = float(entry["price"])
                    size = float(entry["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                if size > 0:
                    out.append(BookLevel(price=price, size=size))
            return out

        bids = levels("bids")
        asks = levels("asks")
        # Normalize ordering: best bid = highest, best ask = lowest.
        bids.sort(key=lambda lvl: lvl.price, reverse=True)
        asks.sort(key=lambda lvl: lvl.price)
        if not bids and not asks:
            return None
        return OrderBook(token_id=token_id, bids=bids, asks=asks)
