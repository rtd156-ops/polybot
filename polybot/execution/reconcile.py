"""Order reconciliation -- pure logic, unit-testable without any live API.

The official Polymarket market-maker keeper follows a declarative model: each
cycle compute the *desired* set of orders, compare against what's actually open,
and only cancel/place the difference. This minimizes API calls and avoids races
and duplicate orders. We reuse the same idea for live execution.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesiredOrder:
    token_id: str
    side: str          # "BUY" / "SELL"
    price: float
    size: float        # shares


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float


@dataclass
class ReconcilePlan:
    to_cancel: list[str]            # open order ids to cancel
    to_place: list[DesiredOrder]    # new orders to submit
    keep: list[str]                 # open order ids already matching desired


def _key(token_id: str, side: str, price: float, price_tol: float) -> tuple:
    # Bucket price by tolerance so tiny float diffs don't churn orders.
    bucket = round(price / price_tol) if price_tol > 0 else price
    return (token_id, side.upper(), bucket)


def reconcile(
    desired: list[DesiredOrder],
    open_orders: list[OpenOrder],
    *,
    price_tol: float = 0.001,
    size_tol: float = 0.01,
) -> ReconcilePlan:
    """Diff desired vs. open. Match on (token, side, price~). Mismatched size
    => cancel+replace. Returns the minimal set of cancels and placements.
    """
    open_by_key: dict[tuple, list[OpenOrder]] = {}
    for o in open_orders:
        open_by_key.setdefault(_key(o.token_id, o.side, o.price, price_tol), []).append(o)

    to_place: list[DesiredOrder] = []
    keep: list[str] = []
    matched_ids: set[str] = set()

    for d in desired:
        bucket = open_by_key.get(_key(d.token_id, d.side, d.price, price_tol), [])
        match = next(
            (o for o in bucket
             if o.order_id not in matched_ids and abs(o.size - d.size) <= size_tol),
            None,
        )
        if match is not None:
            matched_ids.add(match.order_id)
            keep.append(match.order_id)
        else:
            to_place.append(d)

    to_cancel = [o.order_id for o in open_orders if o.order_id not in matched_ids]
    return ReconcilePlan(to_cancel=to_cancel, to_place=to_place, keep=keep)
