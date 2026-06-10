from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.clients.gamma import GammaClient
from polybot.scanner import filter_market, looks_ambiguous, MarketScanner


def _future(days):
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()


def test_filter_passes_healthy_market(base_config, sample_market):
    res = filter_market(sample_market, base_config)
    assert res.passed, res.reason


def test_filter_rejects_low_volume(base_config, sample_market):
    sample_market.volume_usd = 100
    res = filter_market(sample_market, base_config)
    assert not res.passed and "low_volume" in res.reason


def test_filter_rejects_disallowed_category(base_config, sample_market):
    sample_market.category = "Pop Culture"
    res = filter_market(sample_market, base_config)
    assert not res.passed and "category_not_allowed" in res.reason


def test_filter_rejects_resolves_too_soon(base_config, sample_market):
    sample_market.end_date = _future(0.2)
    res = filter_market(sample_market, base_config)
    assert not res.passed and "too_soon" in res.reason


def test_filter_rejects_missing_tokens(base_config, sample_market):
    sample_market.yes_token_id = None
    res = filter_market(sample_market, base_config)
    assert not res.passed and res.reason == "missing_clob_tokens"


def test_uncategorized_allowed_by_default(base_config, sample_market):
    sample_market.category = None
    res = filter_market(sample_market, base_config)
    assert res.passed, res.reason


def test_uncategorized_blocked_when_disabled(base_config, sample_market):
    base_config.data["scanner"]["allow_uncategorized"] = False
    sample_market.category = None
    res = filter_market(sample_market, base_config)
    assert not res.passed and res.reason == "category_missing"


def test_ambiguous_detection(base_config, sample_market):
    sample_market.question = "Will this subjective thing happen?"
    assert looks_ambiguous(sample_market, ["subjective"])
    res = filter_market(sample_market, base_config)
    assert not res.passed and res.reason == "ambiguous_resolution"


class _FakeGamma:
    def __init__(self, markets):
        self._m = markets

    def fetch_markets(self, limit=400):
        return self._m


def test_scanner_uses_gamma_normalization(base_config, gamma_rows):
    markets = [GammaClient.normalize_market(r) for r in gamma_rows]
    scanner = MarketScanner(base_config, _FakeGamma(markets))
    passing, results = scanner.scan()
    # first row healthy, second is low volume -> only one passes
    assert len(results) == 2
    assert len(passing) == 1
    assert passing[0].market_id == "1"


def test_gamma_parses_jsonish_arrays(gamma_rows):
    m = GammaClient.normalize_market(gamma_rows[0])
    assert m.yes_token_id == "tok-yes"
    assert m.no_token_id == "tok-no"
    assert m.yes_price == 0.45
    assert m.url.endswith("/rain")
