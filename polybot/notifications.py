"""Webhook notifications.

A single generic webhook (Slack/Discord/n8n/Make/your own WhatsApp relay). The
URL and token come from .env only. Every payload is run through ``redact`` before
it leaves the process, so a secret can never be exfiltrated via an alert.

If notifications are disabled or no URL is configured, calls are no-ops -- the
bot runs perfectly fine in analysis/paper with alerts off.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from .config import Config
from .logging_setup import get_logger
from .models import Opportunity, OrderResult, Signal
from .secretsafe import redact

log = get_logger("polybot.notify")


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()

# Canonical event types (keep in sync with config.notifications.events).
EVENTS = (
    "opportunity_detected",
    "signal_rejected",
    "paper_order_opened",
    "paper_order_closed",
    "live_order_created",
    "live_order_filled",
    "risk_block",
    "daily_report",
    "error",
)


class Notifier:
    """Fans events out to any enabled sink: a webhook and/or a local JSONL outbox.

    The outbox is an append-only ``data/outbox.jsonl`` that an external assistant
    (e.g. Rook/OpenClaw) can tail to relay alerts to you -- no inbound network or
    shared secret required. Every payload is redacted before it leaves the process.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.get("notifications", "enabled", default=True))
        self.url = cfg.secrets.webhook_url
        self.token = cfg.secrets.webhook_token
        self.timeout = float(cfg.get("notifications", "timeout_seconds", default=10))
        self.event_flags = cfg.get("notifications", "events", default={}) or {}
        # File outbox (independent of the webhook).
        self.outbox_enabled = bool(cfg.get("notifications", "outbox", "enabled", default=False))
        outbox_path = cfg.get("notifications", "outbox", "path", default="data/outbox.jsonl")
        p = Path(outbox_path)
        self.outbox_path = p if p.is_absolute() else cfg.repo_root / p

    def _event_allowed(self, event_type: str) -> bool:
        # Default to True for unknown/unspecified event types.
        return bool(self.event_flags.get(event_type, True))

    def send(self, event_type: str, payload: dict,
             strategy_id: Optional[str] = None) -> bool:
        """Fan an alert out to every enabled sink. Returns True if any fired.

        ``strategy_id`` is surfaced at the top level (and inside data) so a relay
        like Rook can prefix alerts with [conservative]/[balanced]/[aggressive].
        Never raises -- notification failure must not break the trading loop.
        """
        if not self._event_allowed(event_type):
            return False

        data = redact(payload)
        if strategy_id is not None and isinstance(data, dict):
            data.setdefault("strategy_id", strategy_id)
        body = {"ts": _utcnow(), "strategy_id": strategy_id,
                "event": event_type, "data": data}
        fired = False
        if self.outbox_enabled:
            fired = self._write_outbox(body) or fired
        if self.enabled and self.url:
            fired = self._post_webhook(body) or fired
        return fired

    def _write_outbox(self, body: dict) -> bool:
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, default=str) + "\n")
            return True
        except OSError as exc:
            log.warning("Outbox write failed: %s", type(exc).__name__)
            return False

    def _post_webhook(self, body: dict) -> bool:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.post(
                self.url, data=json.dumps(body, default=str),
                headers=headers, timeout=self.timeout,
            )
            if resp.status_code >= 300:
                log.warning("Webhook %s returned %d", body.get("event"), resp.status_code)
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Webhook %s failed: %s", body.get("event"), type(exc).__name__)
            return False

    # -- typed convenience builders ------------------------------------------
    @staticmethod
    def opportunity_payload(opp: Opportunity) -> dict:
        d = opp.to_dict()
        return {
            "market": d["question"],
            "outcome": d["outcome"],
            "market_price": d["market_price"],
            "estimated_probability": d["estimated_probability"],
            "edge": d["edge"],
            "confidence": d["confidence"],
            "liquidity": d["useful_liquidity_usd"],
            "spread": d["spread"],
            "suggested_action": d["suggested_action"],
            "risk_reason": None,
            "reasons": d["reasons"],
            "url": d["url"],
        }

    @staticmethod
    def signal_rejected_payload(sig: Signal) -> dict:
        d = sig.opportunity.to_dict()
        return {
            "market": d["question"],
            "outcome": d["outcome"],
            "market_price": d["market_price"],
            "estimated_probability": d["estimated_probability"],
            "edge": d["edge"],
            "confidence": d["confidence"],
            "liquidity": d["useful_liquidity_usd"],
            "spread": d["spread"],
            "suggested_action": "BLOCKED",
            "risk_reason": sig.reason,
            "url": d["url"],
        }

    @staticmethod
    def order_payload(order: OrderResult, market_question: str, url: str) -> dict:
        d = order.to_dict()
        return {
            "market": market_question,
            "outcome": d["outcome"],
            "side": d["side"],
            "price": d["price"],
            "size": d["size"],
            "notional_usd": d["notional_usd"],
            "status": d["status"],
            "is_paper": d["is_paper"],
            "url": url,
        }
