from __future__ import annotations

from polybot.models import BookLevel
from polybot.pricing import exchange_fee_usd, slippage_bps, walk_book


def test_walk_single_level():
    fill = walk_book([BookLevel(0.50, 1000)], 25.0)
    assert fill.fully_filled
    assert fill.avg_price == 0.50
    assert abs(fill.filled_size - 50.0) < 1e-9
    assert fill.levels_consumed == 1


def test_walk_multiple_levels_avg_price():
    # $5 at 0.50 (10 shares) then $20 at 0.60 (33.33 shares)
    levels = [BookLevel(0.50, 10), BookLevel(0.60, 1000)]
    fill = walk_book(levels, 25.0)
    assert fill.fully_filled
    assert 0.50 < fill.avg_price < 0.60
    assert fill.levels_consumed == 2


def test_walk_runs_out_of_liquidity():
    fill = walk_book([BookLevel(0.50, 10)], 25.0)  # only $5 available
    assert not fill.fully_filled
    assert fill.filled_size == 10


def test_walk_empty_book():
    fill = walk_book([], 25.0)
    assert fill.filled_size == 0 and not fill.fully_filled


def test_fee_formula_symmetric_around_half():
    # min(p, 1-p): fee at 0.2 equals fee at 0.8
    assert abs(exchange_fee_usd(100, 0.2, 100) - exchange_fee_usd(100, 0.8, 100)) < 1e-9


def test_fee_zero_when_bps_zero():
    assert exchange_fee_usd(0, 0.5, 100) == 0.0


def test_slippage_bps():
    assert abs(slippage_bps(0.46, 0.45) - (0.01 / 0.45 * 10000)) < 1e-6
    assert slippage_bps(0.46, None) == 0.0
