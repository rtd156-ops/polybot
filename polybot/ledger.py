"""SQLite ledger -- the auditable system of record.

Tables: markets, opportunities, signals, paper_orders, live_orders, positions,
fills, risk_rejections, balance_snapshots, events.

Design notes:
  * One file, WAL mode, survives restarts.
  * No secrets are ever written here (callers pass scrubbed payloads).
  * All writes go through typed helpers so reports stay stable.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .logging_setup import get_logger
from .models import (
    Market,
    Opportunity,
    OrderResult,
    OrderSide,
    OrderStatus,
    Outcome,
    Signal,
)

log = get_logger("polybot.ledger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    question TEXT,
    slug TEXT,
    category TEXT,
    volume_usd REAL,
    liquidity_usd REAL,
    end_date TEXT,
    url TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    market_id TEXT,
    question TEXT,
    category TEXT,
    outcome TEXT,
    market_price REAL,
    estimated_probability REAL,
    edge REAL,
    confidence REAL,
    spread REAL,
    useful_liquidity_usd REAL,
    suggested_action TEXT,
    reasons TEXT,
    url TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    market_id TEXT,
    outcome TEXT,
    accepted INTEGER,
    reason TEXT,
    side TEXT,
    target_price REAL,
    notional_usd REAL
);
CREATE TABLE IF NOT EXISTS risk_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    market_id TEXT,
    outcome TEXT,
    reason TEXT,
    edge REAL,
    confidence REAL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    created_at TEXT,
    market_id TEXT,
    token_id TEXT,
    outcome TEXT,
    side TEXT,
    price REAL,
    size REAL,
    filled_size REAL,
    notional_usd REAL,
    status TEXT,
    reason TEXT,
    fee_usd REAL DEFAULT 0,
    slippage_bps REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS live_orders (
    order_id TEXT PRIMARY KEY,
    created_at TEXT,
    market_id TEXT,
    token_id TEXT,
    outcome TEXT,
    side TEXT,
    price REAL,
    size REAL,
    filled_size REAL,
    notional_usd REAL,
    status TEXT,
    reason TEXT,
    fee_usd REAL DEFAULT 0,
    slippage_bps REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT,
    closed_at TEXT,
    market_id TEXT,
    category TEXT,
    token_id TEXT,
    outcome TEXT,
    size REAL,
    entry_price REAL,
    exit_price REAL,
    notional_usd REAL,
    realized_pnl REAL,
    status TEXT,
    is_paper INTEGER
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    order_id TEXT,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    is_paper INTEGER,
    fee_usd REAL DEFAULT 0,
    slippage_bps REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    cash_usd REAL,
    blocked_usd REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    equity_usd REAL,
    is_paper INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    event_type TEXT,
    severity TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_created ON opportunities(created_at);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
"""


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


class Ledger:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._tx() as cur:
            cur.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created. Idempotent.

        This is also how fresh DBs get the strategy_id column: tables are created
        without it, then it's added here -- so old and new DBs converge safely.
        Existing rows default to strategy_id='default'.
        """
        sid = ("strategy_id", "TEXT DEFAULT 'default'")
        wanted = {
            "opportunities": [sid],
            "signals": [sid],
            "risk_rejections": [sid],
            "paper_orders": [("fee_usd", "REAL DEFAULT 0"), ("slippage_bps", "REAL DEFAULT 0"), sid],
            "live_orders": [("fee_usd", "REAL DEFAULT 0"), ("slippage_bps", "REAL DEFAULT 0"), sid],
            "positions": [sid],
            "fills": [("fee_usd", "REAL DEFAULT 0"), ("slippage_bps", "REAL DEFAULT 0"), sid],
            "balance_snapshots": [sid],
            "events": [sid],
        }
        with self._tx() as cur:
            for table, cols in wanted.items():
                existing = {r["name"] for r in cur.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in existing:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        self._conn.close()

    # -- writes ---------------------------------------------------------------
    def upsert_market(self, m: Market) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO markets(market_id,condition_id,question,slug,category,
                   volume_usd,liquidity_usd,end_date,url,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(market_id) DO UPDATE SET
                     volume_usd=excluded.volume_usd,
                     liquidity_usd=excluded.liquidity_usd,
                     end_date=excluded.end_date,
                     last_seen=excluded.last_seen""",
                (m.market_id, m.condition_id, m.question, m.slug, m.category,
                 m.volume_usd, m.liquidity_usd, m.end_date, m.url, _now()),
            )

    def record_opportunity(self, opp: Opportunity, strategy_id: str = "default") -> int:
        d = opp.to_dict()
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO opportunities(created_at,market_id,question,category,
                   outcome,market_price,estimated_probability,edge,confidence,spread,
                   useful_liquidity_usd,suggested_action,reasons,url,strategy_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["created_at"], d["market_id"], d["question"], d["category"],
                 d["outcome"], d["market_price"], d["estimated_probability"],
                 d["edge"], d["confidence"], d["spread"], d["useful_liquidity_usd"],
                 d["suggested_action"], json.dumps(d["reasons"]), d["url"], strategy_id),
            )
            return int(cur.lastrowid)

    def record_signal(self, sig: Signal, strategy_id: str = "default") -> None:
        opp = sig.opportunity
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO signals(created_at,market_id,outcome,accepted,reason,
                   side,target_price,notional_usd,strategy_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                (sig.created_at, opp.market.market_id, opp.outcome.value,
                 1 if sig.accepted else 0, sig.reason,
                 sig.side.value if sig.side else None,
                 sig.target_price, sig.notional_usd, strategy_id),
            )
            if not sig.accepted:
                cur.execute(
                    """INSERT INTO risk_rejections(created_at,market_id,outcome,reason,
                       edge,confidence,strategy_id) VALUES(?,?,?,?,?,?,?)""",
                    (sig.created_at, opp.market.market_id, opp.outcome.value,
                     sig.reason, opp.estimate.edge, opp.estimate.confidence, strategy_id),
                )

    def record_order(self, order: OrderResult, strategy_id: str = "default") -> None:
        table = "paper_orders" if order.is_paper else "live_orders"
        d = order.to_dict()
        with self._tx() as cur:
            cur.execute(
                f"""INSERT OR REPLACE INTO {table}(order_id,created_at,market_id,
                    token_id,outcome,side,price,size,filled_size,notional_usd,
                    status,reason,fee_usd,slippage_bps,strategy_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d["order_id"], d["created_at"], d["market_id"], d["token_id"],
                 d["outcome"], d["side"], d["price"], d["size"], d["filled_size"],
                 d["notional_usd"], d["status"], d["reason"],
                 d.get("fee_usd", 0.0), d.get("slippage_bps", 0.0), strategy_id),
            )

    def record_fill(self, order: OrderResult, strategy_id: str = "default") -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO fills(created_at,order_id,market_id,token_id,side,
                   price,size,is_paper,fee_usd,slippage_bps,strategy_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (_now(), order.order_id, order.market_id, order.token_id,
                 order.side.value, order.price, order.filled_size,
                 1 if order.is_paper else 0, order.fee_usd, order.slippage_bps, strategy_id),
            )

    def open_position(self, *, market: Market, order: OrderResult,
                      strategy_id: str = "default") -> int:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO positions(opened_at,market_id,category,token_id,outcome,
                   size,entry_price,notional_usd,realized_pnl,status,is_paper,strategy_id)
                   VALUES(?,?,?,?,?,?,?,?,0,?,?,?)""",
                (order.created_at, market.market_id, market.category or "Uncategorized",
                 order.token_id, order.outcome.value, order.filled_size, order.price,
                 order.notional_usd, OrderStatus.OPEN.value,
                 1 if order.is_paper else 0, strategy_id),
            )
            return int(cur.lastrowid)

    def close_position(self, position_id: int, exit_price: float) -> Optional[float]:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT size, entry_price FROM positions WHERE id=? AND status=?",
                (position_id, OrderStatus.OPEN.value),
            ).fetchone()
            if row is None:
                return None
            pnl = (exit_price - row["entry_price"]) * row["size"]
            cur.execute(
                """UPDATE positions SET closed_at=?, exit_price=?, realized_pnl=?,
                   status=? WHERE id=?""",
                (_now(), exit_price, pnl, OrderStatus.CLOSED.value, position_id),
            )
            return pnl

    def snapshot_balance(self, *, cash: float, blocked: float, unrealized: float,
                         realized: float, is_paper: bool = True,
                         strategy_id: str = "default") -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO balance_snapshots(created_at,cash_usd,blocked_usd,
                   unrealized_pnl,realized_pnl,equity_usd,is_paper,strategy_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (_now(), cash, blocked, unrealized, realized,
                 cash + blocked + unrealized, 1 if is_paper else 0, strategy_id),
            )

    def record_event(self, event_type: str, payload: dict, severity: str = "info",
                     strategy_id: str = "default") -> None:
        """Audit any notable event. Payload must already be secret-scrubbed."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO events(created_at,event_type,severity,payload,strategy_id)
                   VALUES(?,?,?,?,?)""",
                (_now(), event_type, severity, json.dumps(payload, default=str), strategy_id),
            )

    # -- reads / reports ------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        try:
            return cur.execute(sql, params).fetchall()
        finally:
            cur.close()

    def open_positions(self, is_paper: bool = True,
                       strategy_id: Optional[str] = None) -> list[sqlite3.Row]:
        if strategy_id is None:
            return self.query(
                "SELECT * FROM positions WHERE status=? AND is_paper=? ORDER BY opened_at",
                (OrderStatus.OPEN.value, 1 if is_paper else 0),
            )
        return self.query(
            "SELECT * FROM positions WHERE status=? AND is_paper=? AND strategy_id=? "
            "ORDER BY opened_at",
            (OrderStatus.OPEN.value, 1 if is_paper else 0, strategy_id),
        )

    def strategy_ids(self) -> list[str]:
        """Distinct strategy ids that appear anywhere in the ledger."""
        rows = self.query(
            "SELECT DISTINCT strategy_id s FROM positions "
            "UNION SELECT DISTINCT strategy_id s FROM paper_orders "
            "UNION SELECT DISTINCT strategy_id s FROM opportunities"
        )
        return sorted({r["s"] for r in rows if r["s"]})

    def risk_state_inputs(
        self, is_paper: bool = True, starting_balance: float = 0.0,
        strategy_id: str = "default",
    ) -> dict[str, Any]:
        """Aggregate exposure, daily counters, and equity for ONE strategy.

        Every figure is scoped to ``strategy_id`` so each strategy's risk is
        evaluated against its own book, never the global portfolio.
        """
        positions = self.open_positions(is_paper, strategy_id=strategy_id)
        by_cat: dict[str, float] = {}
        by_mkt: dict[str, float] = {}
        total = 0.0
        for p in positions:
            total += p["notional_usd"]
            by_cat[p["category"]] = by_cat.get(p["category"], 0.0) + p["notional_usd"]
            by_mkt[p["market_id"]] = by_mkt.get(p["market_id"], 0.0) + p["notional_usd"]

        today = _today()
        ip = 1 if is_paper else 0
        new_today = self.query(
            "SELECT COUNT(*) c FROM positions WHERE substr(opened_at,1,10)=? "
            "AND is_paper=? AND strategy_id=?",
            (today, ip, strategy_id),
        )[0]["c"]
        realized_today = self.query(
            """SELECT COALESCE(SUM(realized_pnl),0) p FROM positions
               WHERE substr(closed_at,1,10)=? AND is_paper=? AND strategy_id=?""",
            (today, ip, strategy_id),
        )[0]["p"]
        realized_total = self.query(
            "SELECT COALESCE(SUM(realized_pnl),0) p FROM positions "
            "WHERE is_paper=? AND strategy_id=?",
            (ip, strategy_id),
        )[0]["p"]
        peak_snapshot = self.query(
            "SELECT COALESCE(MAX(equity_usd),0) e FROM balance_snapshots "
            "WHERE is_paper=? AND strategy_id=?",
            (ip, strategy_id),
        )[0]["e"]

        current_equity = float(starting_balance) + float(realized_total)
        peak_equity = max(float(starting_balance), float(peak_snapshot), current_equity)
        # Free bankroll for Kelly = equity not already committed to open positions.
        bankroll = max(0.0, current_equity - total)
        return {
            "total_exposure_usd": total,
            "exposure_by_category": by_cat,
            "exposure_by_market": by_mkt,
            "new_positions_today": int(new_today),
            "realized_pnl_today": float(realized_today),
            "bankroll_usd": bankroll,
            "peak_equity_usd": peak_equity,
            "current_equity_usd": current_equity,
        }
