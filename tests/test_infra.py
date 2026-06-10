"""Tests for cross-cutting robustness infra: rate limiter, reconciler, WS parse."""

from __future__ import annotations

import time

from polybot.clients.http import TokenBucket
from polybot.clients.ws import parse_book_message
from polybot.execution.reconcile import DesiredOrder, OpenOrder, reconcile


# --- TokenBucket -------------------------------------------------------------

def test_token_bucket_allows_burst_up_to_capacity():
    tb = TokenBucket(rate_per_sec=100, capacity=5)
    start = time.monotonic()
    for _ in range(5):
        tb.acquire()
    # 5 tokens available immediately -> effectively no wait.
    assert time.monotonic() - start < 0.05


def test_token_bucket_throttles_beyond_capacity():
    tb = TokenBucket(rate_per_sec=20, capacity=1)  # ~50ms between tokens
    tb.acquire()  # consume the one token
    start = time.monotonic()
    tb.acquire()  # must wait ~1/20s
    assert time.monotonic() - start >= 0.03


def test_token_bucket_disabled_is_noop():
    tb = TokenBucket(rate_per_sec=0)
    start = time.monotonic()
    for _ in range(100):
        tb.acquire()
    assert time.monotonic() - start < 0.05


# --- Order reconciliation ----------------------------------------------------

def test_reconcile_keeps_matching_orders():
    desired = [DesiredOrder("t1", "BUY", 0.40, 100)]
    open_orders = [OpenOrder("o1", "t1", "BUY", 0.40, 100)]
    plan = reconcile(desired, open_orders)
    assert plan.keep == ["o1"]
    assert plan.to_cancel == [] and plan.to_place == []


def test_reconcile_cancels_stale_and_places_new():
    desired = [DesiredOrder("t1", "BUY", 0.45, 100)]
    open_orders = [OpenOrder("o1", "t1", "BUY", 0.40, 100)]  # wrong price
    plan = reconcile(desired, open_orders)
    assert plan.to_cancel == ["o1"]
    assert len(plan.to_place) == 1 and plan.to_place[0].price == 0.45


def test_reconcile_size_mismatch_replaces():
    desired = [DesiredOrder("t1", "BUY", 0.40, 200)]
    open_orders = [OpenOrder("o1", "t1", "BUY", 0.40, 100)]
    plan = reconcile(desired, open_orders)
    assert plan.to_cancel == ["o1"] and len(plan.to_place) == 1


def test_reconcile_cancels_orphans():
    plan = reconcile([], [OpenOrder("o1", "t1", "BUY", 0.40, 100)])
    assert plan.to_cancel == ["o1"]


# --- WebSocket book parsing --------------------------------------------------

def test_parse_ws_book_message():
    msg = {
        "event_type": "book", "asset_id": "tok-1",
        "bids": [{"price": "0.44", "size": "100"}, {"price": "0.43", "size": "50"}],
        "asks": [{"price": "0.46", "size": "80"}],
    }
    book = parse_book_message(msg)
    assert book is not None
    assert book.best_bid == 0.44 and book.best_ask == 0.46
    assert book.token_id == "tok-1"


def test_parse_ws_ignores_non_book_events():
    assert parse_book_message({"event_type": "price_change"}) is None


def test_parse_ws_empty_returns_none():
    assert parse_book_message({"event_type": "book", "asset_id": "t"}) is None
