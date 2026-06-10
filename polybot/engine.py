"""Engine -- orchestrates one scan/analysis/paper cycle and the loop.

Pipeline per cycle:
    scan markets -> for each survivor: fetch book -> price -> estimate prob
      -> emit Opportunity (if edge/conf clear) -> risk check -> Signal
        -> (paper) place order + open position -> persist + alert everything

The engine codes only against ``ExecutionClient``; paper vs live is chosen once
at construction based on config. In analysis mode no orders are placed at all.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Optional

from .arbitrage import detect_arbitrage
from .config import Config
from .clients.clob import ClobDataClient
from .clients.gamma import GammaClient
from .clients.http import HttpClient
from .execution.base import ExecutionClient
from .execution.paper import PaperExecution
from .ledger import Ledger
from .logging_setup import get_logger
from .models import (
    Market,
    Opportunity,
    Outcome,
    OrderSide,
)
from .notifications import Notifier
from .pricing import build_snapshot, is_tradeable
from .probability import ProbabilityModel, passes_thresholds
from .risk import RiskManager, RiskState
from .scanner import MarketScanner

log = get_logger("polybot.engine")


class CycleStats:
    def __init__(self) -> None:
        self.markets_scanned = 0
        self.markets_passed = 0
        self.opportunities = 0
        self.signals_accepted = 0
        self.signals_rejected = 0
        self.paper_orders = 0
        self.errors = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class Engine:
    def __init__(self, cfg: Config, ledger: Optional[Ledger] = None) -> None:
        self.cfg = cfg
        http = HttpClient(
            timeout=float(cfg.get("data_sources", "http_timeout_seconds", default=15)),
            max_retries=int(cfg.get("data_sources", "http_max_retries", default=3)),
            backoff=float(cfg.get("data_sources", "http_backoff_seconds", default=1.5)),
            user_agent=str(cfg.get("data_sources", "user_agent", default="polybot/1.0")),
            rate_limit_per_sec=float(cfg.get("data_sources", "rate_limit_per_sec", default=0)),
        )
        self.gamma = GammaClient(cfg.get("data_sources", "gamma_base_url"), http)
        self.clob = ClobDataClient(cfg.get("data_sources", "clob_base_url"), http)
        self.scanner = MarketScanner(cfg, self.gamma)
        self.model = ProbabilityModel(cfg)
        self.risk = RiskManager(cfg)
        self.notifier = Notifier(cfg)
        self.ledger = ledger or Ledger(cfg.db_path())
        self.executor: ExecutionClient = self._build_executor()
        self._stop = threading.Event()
        self.arb_enabled = bool(cfg.get("arbitrage", "enabled", default=False))

    def _build_executor(self) -> ExecutionClient:
        """Choose execution backend. Live only if fully cleared, else paper."""
        if self.cfg.live_truly_enabled:
            from .execution.live import PolymarketLiveExecution
            log.warning("Constructing LIVE executor (real orders enabled)")
            return PolymarketLiveExecution(self.cfg)
        return PaperExecution(self.cfg)

    # -- public API -----------------------------------------------------------
    def run_once(self) -> CycleStats:
        stats = CycleStats()
        try:
            passing, results = self.scanner.scan()
        except Exception as exc:
            log.exception("Scan failed")
            self.notifier.send("error", {"stage": "scan", "error": str(exc)})
            stats.errors += 1
            return stats

        stats.markets_scanned = len(results)
        stats.markets_passed = len(passing)

        top_n = int(self.cfg.get("engine", "scan_top_n_for_book", default=60))
        for market in passing[:top_n]:
            try:
                self.ledger.upsert_market(market)
                self._process_market(market, stats)
            except Exception as exc:
                stats.errors += 1
                log.exception("Error processing market %s", market.market_id)
                self.notifier.send(
                    "error", {"stage": "process", "market_id": market.market_id,
                              "error": str(exc)}
                )

        self._snapshot(stats)
        log.info("Cycle complete: %s", stats.as_dict())
        return stats

    def request_stop(self, *_args) -> None:
        """Signal the loop to finish the current cycle and exit cleanly."""
        if not self._stop.is_set():
            log.info("Shutdown requested; will stop after the current cycle.")
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT so `systemctl stop` shuts down gracefully.

        Only valid in the main thread; ignored otherwise (e.g. under pytest).
        """
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
        except (ValueError, RuntimeError):
            pass

    def run_loop(self, max_cycles: Optional[int] = None) -> None:
        interval = int(self.cfg.get("engine", "loop_interval_seconds", default=300))
        cycle = 0
        self.install_signal_handlers()
        log.info("Starting loop (mode=%s, interval=%ds)", self.cfg.mode, interval)
        while not self._stop.is_set():
            self.run_once()
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            # Interruptible sleep: wake immediately on shutdown signal.
            if self._stop.wait(timeout=interval):
                break
        log.info("Loop stopped after %d cycle(s); shutting down.", cycle)

    # -- per-market pipeline --------------------------------------------------
    def _process_market(self, market: Market, stats: CycleStats) -> None:
        # Evaluate the YES outcome (NO is its complement for a binary market).
        outcome = Outcome.YES
        book = self.clob.fetch_book(market.yes_token_id or "")
        snap = build_snapshot(self.cfg, market, outcome, book)

        # Optional risk-free arbitrage check (YES_ask + NO_ask < 1).
        if self.arb_enabled:
            self._check_arbitrage(market, book, stats)

        if not is_tradeable(self.cfg, snap):
            return  # cannot enter+exit reasonably -> not an actionable opportunity

        est = self.model.estimate(market, outcome, snap, book)
        if est is None or not passes_thresholds(self.cfg, est):
            return

        # Direction: positive edge => buy YES; negative => buy NO.
        if est.edge >= 0:
            action_outcome, action = Outcome.YES, "BUY YES"
        else:
            action_outcome, action = Outcome.NO, "BUY NO"

        opp = Opportunity(
            market=market, outcome=action_outcome, pricing=snap,
            estimate=est, suggested_action=action,
        )
        self.ledger.record_opportunity(opp)
        stats.opportunities += 1
        self.notifier.send("opportunity_detected", self.notifier.opportunity_payload(opp))
        log.info(
            "Opportunity: %s %s edge=%.3f conf=%.2f", action,
            market.question[:60], est.edge, est.confidence,
        )

        # Risk evaluation against current portfolio state (incl. equity/peak).
        start = float(self.cfg.get("paper", "starting_balance_usd", default=1000))
        state = RiskState(
            **self.ledger.risk_state_inputs(
                is_paper=self.executor.is_paper, starting_balance=start
            )
        )
        signal = self.risk.evaluate(opp, state)
        self.ledger.record_signal(signal)
        if not signal.accepted:
            stats.signals_rejected += 1
            self.notifier.send("risk_block", self.notifier.signal_rejected_payload(signal))
            return
        stats.signals_accepted += 1

        # In analysis mode we stop here -- no orders, not even simulated.
        if self.cfg.mode == "analysis":
            return

        self._execute_paper(market, opp, signal, book, stats)

    def _check_arbitrage(self, market: Market, yes_book, stats: CycleStats) -> None:
        """Detect & record a risk-free YES+NO < $1 arbitrage, if any."""
        no_book = self.clob.fetch_book(market.no_token_id or "")
        arb = detect_arbitrage(self.cfg, market, yes_book, no_book)
        if arb is None:
            return
        self.ledger.record_event("arbitrage_detected", arb.to_dict(), severity="info")
        self.notifier.send("opportunity_detected", {
            "market": arb.question, "outcome": "YES+NO", "market_price": arb.combined_cost,
            "edge": arb.net_edge, "suggested_action": "ARBITRAGE BUY YES+NO",
            "liquidity": arb.notional_usd, "url": arb.url,
        })
        log.info("ARBITRAGE: %s combined=%.4f net_edge=%.4f $%.0f",
                 market.question[:50], arb.combined_cost, arb.net_edge, arb.notional_usd)

    def _execute_paper(self, market, opp, signal, book, stats: CycleStats) -> None:
        order = self.executor.place_limit_order(
            market_id=market.market_id,
            token_id=signal.token_id or "",
            outcome=opp.outcome,
            side=OrderSide.BUY,
            price=signal.target_price or (opp.pricing.best_ask or 0.5),
            notional_usd=signal.notional_usd or 0.0,
            reference_book=book,
        )
        self.ledger.record_order(order)
        if order.status.value not in ("FILLED", "PARTIAL"):
            return
        self.ledger.record_fill(order)
        self.ledger.open_position(market=market, order=order)
        stats.paper_orders += 1

        event = "paper_order_opened" if order.is_paper else "live_order_created"
        self.notifier.send(event, self.notifier.order_payload(order, market.question, market.url))

    # -- balance snapshot -----------------------------------------------------
    def _snapshot(self, stats: CycleStats) -> None:
        try:
            inputs = self.ledger.risk_state_inputs(is_paper=self.executor.is_paper)
            start = float(self.cfg.get("paper", "starting_balance_usd", default=1000))
            blocked = inputs["total_exposure_usd"]
            realized = self.ledger.query(
                "SELECT COALESCE(SUM(realized_pnl),0) p FROM positions WHERE is_paper=?",
                (1 if self.executor.is_paper else 0,),
            )[0]["p"]
            self.ledger.snapshot_balance(
                cash=start - blocked + float(realized),
                blocked=blocked, unrealized=0.0, realized=float(realized),
                is_paper=self.executor.is_paper,
            )
        except Exception:
            log.exception("Balance snapshot failed")

    def close(self) -> None:
        self.ledger.close()
        self.executor.close()
