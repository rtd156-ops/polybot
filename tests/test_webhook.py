from __future__ import annotations

from polybot.config import Config, Secrets, DEFAULTS, deep_merge
from polybot.models import Opportunity, Outcome, PricingSnapshot, ProbabilityEstimate
from polybot.notifications import Notifier
from polybot.secretsafe import redact, mask_value


def _cfg(url=None, enabled=True):
    return Config(
        data=deep_merge(DEFAULTS, {"notifications": {"enabled": enabled}}),
        secrets=Secrets(webhook_url=url, webhook_token="secrettoken"),
    )


def _opp(sample_market):
    snap = PricingSnapshot(sample_market.market_id, "ty", Outcome.YES, 0.44, 0.46,
                           0.45, 0.02, 0.45, 4000.0)
    est = ProbabilityEstimate(0.55, 0.45, 0.10, 0.7, ["edge present"])
    return Opportunity(sample_market, Outcome.YES, snap, est, "BUY YES")


def test_no_op_when_disabled(sample_market):
    n = Notifier(_cfg(url="https://example.com", enabled=False))
    assert n.send("opportunity_detected", {"x": 1}) is False


def test_no_op_without_url(sample_market):
    n = Notifier(_cfg(url=None))
    assert n.send("opportunity_detected", {"x": 1}) is False


def test_opportunity_payload_shape(sample_market):
    n = Notifier(_cfg(url="https://example.com"))
    payload = n.opportunity_payload(_opp(sample_market))
    for key in ("market", "outcome", "market_price", "estimated_probability",
                "edge", "confidence", "liquidity", "spread", "suggested_action",
                "url"):
        assert key in payload
    assert payload["outcome"] == "YES"
    assert payload["url"].startswith("https://polymarket.com")


def test_redaction_masks_secrets():
    dirty = {
        "api_key": "abcdef1234567890",
        "note": "key is 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef now",
        "nested": {"private_key": "0x" + "a" * 64},
    }
    clean = redact(dirty)
    assert clean["api_key"].startswith("***REDACTED***")
    assert "0xdeadbeef" not in clean["note"]
    assert "a" * 64 not in clean["nested"]["private_key"]


def test_mask_value_short():
    assert mask_value("abc") == "***REDACTED***"
    assert "len=" in mask_value("a-fairly-long-token-value")


def test_send_actually_posts_redacted(monkeypatch, sample_market):
    captured = {}

    class _Resp:
        status_code = 200

    def fake_post(url, data, headers, timeout):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    import polybot.notifications as nt
    monkeypatch.setattr(nt.requests, "post", fake_post)

    n = Notifier(_cfg(url="https://example.com"))
    payload = {"api_key": "supersecretapikeyvalue123456", "ok": True}
    assert n.send("opportunity_detected", payload) is True
    assert "supersecretapikeyvalue123456" not in captured["data"]
    assert captured["headers"]["Authorization"] == "Bearer secrettoken"
