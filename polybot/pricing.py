"""Orderbook / pricing engine.

Computes mid, spread, implied probability and *useful* liquidity (the notional
you could realistically get in AND out of), and flags pricing-based signals.
The golden rule: never call something tradeable if you can't both enter and exit.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .logging_setup import get_logger
from .models import BookFill, BookLevel, Market, OrderBook, Outcome, PricingSnapshot

log = get_logger("polybot.pricing")


def walk_book(levels: list[BookLevel], target_notional_usd: float) -> BookFill:
    """Consume an order-book side level-by-level to fill a target USD notional.

    Models a *real* taker fill: you eat the best level, then the next, paying a
    worse price as you go -- not a single idealized price. Returns the
    size-weighted average price actually paid and whether the book had enough
    depth. (``levels`` must be sorted best-first.)
    """
    if target_notional_usd <= 0 or not levels:
        return BookFill(0.0, 0.0, 0.0, 0, False)

    remaining = target_notional_usd
    spent = 0.0
    shares = 0.0
    touched = 0
    for lvl in levels:
        if remaining <= 1e-9:
            break
        touched += 1
        level_notional = lvl.price * lvl.size
        if level_notional >= remaining:
            take_shares = remaining / lvl.price
            shares += take_shares
            spent += take_shares * lvl.price
            remaining = 0.0
            break
        shares += lvl.size
        spent += level_notional
        remaining -= level_notional

    if shares <= 0:
        return BookFill(0.0, 0.0, 0.0, touched, False)
    return BookFill(
        avg_price=spent / shares,
        filled_size=shares,
        filled_notional=spent,
        levels_consumed=touched,
        fully_filled=remaining <= 1e-9,
    )


def exchange_fee_usd(fee_bps: float, price: float, shares: float) -> float:
    """Polymarket-style fee: bps/10000 * min(price, 1-price) * shares.

    Fees are symmetric around 0.5 (cheaper at the extremes). Polymarket's fee is
    currently 0, but we model the formula so paper PnL stays honest if it changes.
    """
    if fee_bps <= 0 or shares <= 0 or not (0 < price < 1):
        return 0.0
    return (fee_bps / 10000.0) * min(price, 1.0 - price) * shares


def slippage_bps(fill_price: float, reference_mid: Optional[float]) -> float:
    """Adverse deviation of the fill vs mid, in basis points (always >= 0)."""
    if not reference_mid or reference_mid <= 0:
        return 0.0
    return abs(fill_price - reference_mid) / reference_mid * 10000.0


def build_snapshot(
    cfg: Config,
    market: Market,
    outcome: Outcome,
    book: Optional[OrderBook],
    prior_mid: Optional[float] = None,
) -> PricingSnapshot:
    """Build a pricing snapshot for one outcome from its order book."""
    pc = cfg.section("pricing")
    token_id = market.yes_token_id if outcome is Outcome.YES else market.no_token_id

    best_bid = book.best_bid if book else None
    best_ask = book.best_ask if book else None
    mid = book.mid if book else None
    spread = book.spread if book else None

    # Useful liquidity = the smaller of what you can buy (asks) and later sell
    # (bids). You are only as liquid as your ability to exit.
    if book:
        entry = book.notional_within("ask", levels=3)
        exit_ = book.notional_within("bid", levels=3)
        useful = min(entry, exit_)
    else:
        useful = 0.0

    snap = PricingSnapshot(
        market_id=market.market_id,
        token_id=token_id or "",
        outcome=outcome,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        implied_probability=mid,  # for a YES/NO share, mid price ~= implied prob
        useful_liquidity_usd=useful,
    )

    snap.signals = _detect_signals(cfg, snap, prior_mid, pc)
    return snap


def _detect_signals(
    cfg: Config,
    snap: PricingSnapshot,
    prior_mid: Optional[float],
    pc: dict,
) -> list[str]:
    signals: list[str] = []
    max_spread = float(cfg.get("scanner", "max_spread", default=0.05))

    if snap.spread is not None and snap.spread > max_spread * float(
        pc.get("wide_spread_factor", 1.5)
    ):
        signals.append(f"wide_spread({snap.spread:.3f})")

    if prior_mid is not None and snap.mid is not None:
        move = snap.mid - prior_mid
        if abs(move) >= float(pc.get("strong_move_threshold", 0.10)):
            signals.append(f"strong_move({move:+.3f})")

    if snap.mid is not None:
        lo = float(pc.get("extreme_probability_low", 0.08))
        hi = float(pc.get("extreme_probability_high", 0.92))
        if snap.mid <= lo or snap.mid >= hi:
            signals.append(f"extreme_probability({snap.mid:.3f})")

    if snap.useful_liquidity_usd < float(pc.get("min_useful_liquidity_usd", 250)):
        signals.append(f"thin_useful_liquidity({snap.useful_liquidity_usd:.0f})")

    return signals


def is_tradeable(cfg: Config, snap: PricingSnapshot) -> bool:
    """Can we realistically enter AND exit this outcome right now?"""
    pc = cfg.section("pricing")
    if snap.best_bid is None or snap.best_ask is None:
        return False
    if snap.spread is None or snap.spread > float(
        cfg.get("scanner", "max_spread", default=0.05)
    ):
        return False
    if snap.useful_liquidity_usd < float(pc.get("min_useful_liquidity_usd", 250)):
        return False
    return True
