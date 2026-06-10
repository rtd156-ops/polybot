"""Shared fixtures. Everything here is offline -- no network, no real secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.config import Config, Secrets, DEFAULTS, deep_merge
from polybot.models import BookLevel, Market, OrderBook, Outcome


@pytest.fixture()
def base_config(tmp_path) -> Config:
    data = deep_merge(DEFAULTS, {
        "mode": "paper",
        "scanner": {
            "min_volume_usd": 10000,
            "min_liquidity_usd": 5000,
            "max_spread": 0.05,
            "allowed_categories": ["Crypto", "Politics"],
            "ambiguous_keywords": ["subjective"],
            "min_days_to_resolution": 1,
            "max_days_to_resolution": 180,
        },
        "storage": {"db_path": str(tmp_path / "test.db")},
    })
    return Config(data=data, secrets=Secrets(), repo_root=tmp_path)


@pytest.fixture()
def sample_market() -> Market:
    return Market(
        market_id="123",
        condition_id="0xcond",
        question="Will BTC be above $100k by year end?",
        slug="btc-100k",
        category="Crypto",
        active=True,
        closed=False,
        volume_usd=500000.0,
        liquidity_usd=80000.0,
        end_date="2026-09-01T00:00:00Z",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        yes_price=0.45,
        no_price=0.55,
        url="https://polymarket.com/market/btc-100k",
    )


@pytest.fixture()
def healthy_book() -> OrderBook:
    return OrderBook(
        token_id="tok-yes",
        bids=[BookLevel(0.44, 2000), BookLevel(0.43, 1500), BookLevel(0.42, 1000)],
        asks=[BookLevel(0.46, 2000), BookLevel(0.47, 1500), BookLevel(0.48, 1000)],
    )


@pytest.fixture()
def gamma_rows() -> list[dict]:
    """Raw Gamma-shaped rows, including the JSON-encoded array quirk."""
    return [
        {
            "id": "1", "conditionId": "0xa", "question": "Will it rain?",
            "slug": "rain", "category": "Crypto", "active": True, "closed": False,
            "volume": "500000", "liquidity": "80000", "endDate": "2026-09-01T00:00:00Z",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.45", "0.55"]),
            "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        },
        {
            "id": "2", "conditionId": "0xb", "question": "Low volume market",
            "slug": "low", "category": "Crypto", "active": True, "closed": False,
            "volume": "100", "liquidity": "50", "endDate": "2026-09-01T00:00:00Z",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.50", "0.50"]),
            "clobTokenIds": json.dumps(["t2y", "t2n"]),
        },
    ]
