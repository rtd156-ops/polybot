from __future__ import annotations

import sqlite3

import pytest

from polybot.config import Config, Secrets, DEFAULTS, deep_merge
from polybot.ledger import Ledger
from polybot.models import (
    Market, OrderResult, OrderSide, OrderStatus, Outcome,
    Opportunity, PricingSnapshot, ProbabilityEstimate, Signal,
)
from polybot.notifications import Notifier
from polybot.strategies import DEFAULT_STRATEGY_ID, load_strategies


def _cfg(extra=None, tmp_path=None):
    data = deep_merge(DEFAULTS, extra or {})
    if tmp_path is not None:
        data = deep_merge(data, {"storage": {"db_path": str(tmp_path / "t.db")}})
    return Config(data=data, secrets=Secrets(),
                  repo_root=tmp_path or DEFAULTS_ROOT)


DEFAULTS_ROOT = __import__("pathlib").Path("/tmp")


# --- config loading with / without strategies --------------------------------

def test_load_without_strategies_returns_default():
    strats = load_strategies(_cfg())
    assert len(strats) == 1
    assert strats[0].strategy_id == DEFAULT_STRATEGY_ID


def test_load_disabled_section_returns_default():
    cfg = _cfg({"strategies": {"enabled": False, "definitions": {"x": {}}}})
    strats = load_strategies(cfg)
    assert len(strats) == 1 and strats[0].strategy_id == "default"


def test_load_three_strategies_with_overrides():
    cfg = _cfg({"strategies": {"enabled": True, "definitions": {
        "conservative": {"paper": {"starting_balance_usd": 1000},
                         "probability": {"min_edge": 0.07},
                         "risk": {"max_total_exposure_usd": 250, "kelly_multiplier": 0.25}},
        "balanced": {"paper": {"starting_balance_usd": 2000},
                     "probability": {"min_edge": 0.04}},
        "aggressive": {"paper": {"starting_balance_usd": 3000},
                       "probability": {"min_edge": 0.025},
                       "risk": {"max_total_exposure_usd": 2000}},
    }}})
    strats = load_strategies(cfg)
    assert [s.strategy_id for s in strats] == ["conservative", "balanced", "aggressive"]
    cons = strats[0].config
    assert cons.get("paper", "starting_balance_usd") == 1000
    assert cons.get("probability", "min_edge") == 0.07
    assert cons.get("risk", "max_total_exposure_usd") == 250
    # Inherited (not overridden) base values survive:
    assert cons.get("data_sources", "gamma_base_url")  # inherited
    assert strats[2].config.get("risk", "max_total_exposure_usd") == 2000


def test_strategy_overlay_ignores_non_overridable_sections():
    cfg = _cfg({"strategies": {"enabled": True, "definitions": {
        "x": {"data_sources": {"gamma_base_url": "http://evil"}, "paper": {"fee_bps": 5}},
    }}})
    strats = load_strategies(cfg)
    # data_sources override is ignored; paper override applies.
    assert strats[0].config.get("data_sources", "gamma_base_url") != "http://evil"
    assert strats[0].config.get("paper", "fee_bps") == 5


# --- ledger migration adds strategy_id, preserving existing rows -------------

def test_ledger_migration_adds_strategy_id(tmp_path):
    db = tmp_path / "old.db"
    # Simulate an OLD database: positions table without strategy_id.
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE positions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at TEXT, closed_at TEXT, market_id TEXT, category TEXT, token_id TEXT,
        outcome TEXT, size REAL, entry_price REAL, exit_price REAL, notional_usd REAL,
        realized_pnl REAL, status TEXT, is_paper INTEGER)""")
    conn.execute("INSERT INTO positions(market_id,notional_usd,status,is_paper) "
                 "VALUES('m1',25,'OPEN',1)")
    conn.commit()
    conn.close()

    led = Ledger(db)  # opening runs the migration
    cols = {r["name"] for r in led.query("PRAGMA table_info(positions)")}
    assert "strategy_id" in cols
    # Existing row defaulted to 'default' and is still there.
    row = led.query("SELECT strategy_id, notional_usd FROM positions")[0]
    assert row["strategy_id"] == "default"
    assert row["notional_usd"] == 25
    led.close()


# --- risk state separated by strategy ---------------------------------------

def _market():
    return Market("m1", "0x", "Q?", "s", "Crypto", True, False, 5e5, 8e4,
                  "2026-09-01T00:00:00Z", "ty", "tn", 0.45, 0.55, "u")


def _order(oid="o1", notional=25.0):
    return OrderResult(order_id=oid, status=OrderStatus.FILLED, market_id="m1",
                       token_id="ty", outcome=Outcome.YES, side=OrderSide.BUY,
                       price=0.50, size=notional / 0.5, filled_size=notional / 0.5,
                       notional_usd=notional, is_paper=True)


def test_risk_state_isolated_per_strategy(tmp_path):
    led = Ledger(tmp_path / "l.db")
    m = _market()
    led.open_position(market=m, order=_order("a", 25), strategy_id="conservative")
    led.open_position(market=m, order=_order("b", 80), strategy_id="aggressive")

    cons = led.risk_state_inputs(is_paper=True, starting_balance=1000, strategy_id="conservative")
    aggr = led.risk_state_inputs(is_paper=True, starting_balance=3000, strategy_id="aggressive")

    assert cons["total_exposure_usd"] == 25
    assert aggr["total_exposure_usd"] == 80
    assert cons["new_positions_today"] == 1
    assert aggr["new_positions_today"] == 1
    # Bankroll reflects each strategy's own starting balance and exposure.
    assert cons["bankroll_usd"] == 1000 - 25
    assert aggr["bankroll_usd"] == 3000 - 80
    led.close()


# --- paper orders separated by strategy -------------------------------------

def test_paper_orders_tagged_by_strategy(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.record_order(_order("c1"), strategy_id="conservative")
    led.record_order(_order("b1"), strategy_id="balanced")
    cons = led.query("SELECT * FROM paper_orders WHERE strategy_id='conservative'")
    bal = led.query("SELECT * FROM paper_orders WHERE strategy_id='balanced'")
    assert len(cons) == 1 and cons[0]["order_id"] == "c1"
    assert len(bal) == 1 and bal[0]["order_id"] == "b1"
    assert sorted(led.strategy_ids()) == ["balanced", "conservative"]
    led.close()


# --- outbox payload carries strategy_id -------------------------------------

def test_outbox_includes_strategy_id(tmp_path):
    import json
    cfg = Config(
        data=deep_merge(DEFAULTS, {"notifications": {
            "enabled": False, "outbox": {"enabled": True, "path": "out.jsonl"}}}),
        secrets=Secrets(), repo_root=tmp_path,
    )
    n = Notifier(cfg)
    assert n.send("opportunity_detected", {"market": "Q?"}, strategy_id="aggressive") is True
    evt = json.loads((tmp_path / "out.jsonl").read_text().strip())
    assert evt["strategy_id"] == "aggressive"
    assert evt["data"]["strategy_id"] == "aggressive"


# --- per-strategy report aggregation ----------------------------------------

def test_report_counts_per_strategy(tmp_path):
    led = Ledger(tmp_path / "l.db")
    m = _market()
    # conservative: 1 open position; balanced: 1 closed winning position
    led.open_position(market=m, order=_order("a", 25), strategy_id="conservative")
    pid = led.open_position(market=m, order=_order("b", 25), strategy_id="balanced")
    led.close_position(pid, exit_price=0.60)  # entry 0.50 -> profit

    cons_open = led.open_positions(is_paper=True, strategy_id="conservative")
    bal_closed = led.query(
        "SELECT realized_pnl FROM positions WHERE status='CLOSED' AND strategy_id='balanced'")
    assert len(cons_open) == 1
    assert len(bal_closed) == 1 and bal_closed[0]["realized_pnl"] > 0
    # conservative has no closed positions
    assert led.query("SELECT COUNT(*) c FROM positions WHERE status='CLOSED' "
                     "AND strategy_id='conservative'")[0]["c"] == 0
    led.close()
