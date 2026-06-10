"""Engine -- orchestrates one scan/analysis/paper cycle and the loop.

Pipeline per cycle (per strategy):
    fetch markets (once, shared) -> filter (per strategy) -> for each survivor:
      fetch book (cached, shared) -> price -> estimate prob -> Opportunity
        -> risk check (per-strategy state) -> Signal -> (paper) order + position
          -> persist + alert, everything tagged with strategy_id.

Multi-strategy: when ``strategies.enabled`` is true, each definition runs as an
independent paper book in the same process. Otherwise a single implicit strategy
"default" runs and behaves exactly as before. The engine codes only against
``ExecutionClient``; live (single-strategy only) is chosen at construction.
"""

from __future__ import annotations

import signal
import threading
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
from .models import Market, Opportunity, Outcome, OrderSide
from .notifications import Notifier
from .pricing import build_snapshot, is_tradeable
from .probability import ProbabilityModel, passes_thresholds
from .risk import RiskManager, RiskState
from .scanner import MarketScanner, filter_market
from .strategies import Strategy, load_strategies

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

    def add(self, other: "CycleStats") -> None:
        self.markets_passed = max(self.markets_passed, other.markets_passed)
        self.opportunities += other.opportunities
        self.signals_accepted += other.signals_accepted
        self.signals_rejected += other.signals_rejected
        self.paper_orders += other.paper_orders
        self.errors += other.errors

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
        self.notifier = Notifier(cfg)
        self.ledger = ledger or Ledger(cfg.db_path())
        self._stop = threading.Event()
        self._book_cache: dict[str, object] = {}

        # Per-strategy components. Single "default" strategy unless enabled.
        self.strategies: list[Strategy] = load_strategies(cfg)
        self.strategies_enabled = bool(cfg.get("strategies", "enabled", default=False))
        self._models: dict[str, ProbabilityModel] = {}
        self._risk: dict[str, RiskManager] = {}
        self._exec: dict[str, ExecutionClient] = {}
        for s in self.strategies:
            self._models[s.strategy_id] = ProbabilityModel(s.config)
            self._risk[s.strategy_id] = RiskManager(s.config)
            self._exec[s.strategy_id] = self._build_executor(s)

        # Arbitrage runs once per cycle (not per strategy) to avoid duplicates.
        # Default off; for multi-strategy keep it explicitly opt-in.
        self.arb_enabled = bool(cfg.get("arbitrage", "enabled", default=False))

    def _build_executor(self, strat: Strategy) -> ExecutionClient:
        """Paper for every strategy. Live only for the single-strategy default."""
        if not self.strategies_enabled and self.cfg.live_truly_enabled:
            from .execution.live import PolymarketLiveExecution
            log.warning("Constructing LIVE executor (real orders enabled)")
            return PolymarketLiveExecution(self.cfg)
        return PaperExecution(strat.config)

    # -- public API -----------------------------------------------------------
    def run_once(self) -> CycleStats:
        stats = CycleStats()
        try:
            raw = self.scanner.fetch_raw()
        except Exception as exc:
            log.exception("Scan failed")
            self.notifier.send("error", {"stage": "scan", "error": str(exc)})
            stats.errors += 1
            return stats

        stats.markets_scanned = len(raw)
        self._book_cache = {}  # shared across strategies within this cycle

        for strat in self.strategies:
            try:
                sstats = self._run_strategy(strat, raw)
                stats.add(sstats)
            except Exception:
                stats.errors += 1
                log.exception("Strategy %s failed", strat.strategy_id)

        # Risk-free arbitrage scan (global, once), if explicitly enabled.
        if self.arb_enabled:
            self._scan_arbitrage(raw, stats)

        self._snapshot_all()
        log.info("Cycle complete: %s", stats.as_dict())
        return stats

    def request_stop(self, *_args) -> None:
        """Signal the loop to finish the current cycle and exit cleanly."""
        if not self._stop.is_set():
            log.info("Shutdown requested; will stop after the current cycle.")
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Handle SIGTERM/SIGINT so `systemctl stop` shuts down gracefully."""
        try:
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
        except (ValueError, RuntimeError):
            pass

    def run_loop(self, max_cycles: Optional[int] = None) -> None:
        interval = int(self.cfg.get("engine", "loop_interval_seconds", default=300))
        cycle = 0
        self.install_signal_handlers()
        log.info("Starting loop (mode=%s, strategies=%d, interval=%ds)",
                 self.cfg.mode, len(self.strategies), interval)
        while not self._stop.is_set():
            self.run_once()
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            if self._stop.wait(timeout=interval):  # interruptible sleep
                break
        log.info("Loop stopped after %d cycle(s); shutting down.", cycle)

    # -- per-strategy pipeline ------------------------------------------------
    def _get_book(self, token_id: str):
        """Fetch a CLOB book once per cycle and reuse across strategies."""
        if not token_id:
            return None
        if token_id not in self._book_cache:
            self._book_cache[token_id] = self.clob.fetch_book(token_id)
        return self._book_cache[token_id]

    def _run_strategy(self, strat: Strategy, raw: list[Market]) -> CycleStats:
        cfg = strat.config
        sid = strat.strategy_id
        executor = self._exec[sid]
        stats = CycleStats()

        passing = [m for m in raw if filter_market(m, cfg).passed]
        stats.markets_passed = len(passing)
        top_n = int(cfg.get("engine", "scan_top_n_for_book", default=60))
        start = float(cfg.get("paper", "starting_balance_usd", default=1000))

        for market in passing[:top_n]:
            try:
                self.ledger.upsert_market(market)
                self._process_market(strat, market, start, executor, stats)
            except Exception:
                stats.errors += 1
                log.exception("[%s] error processing market %s", sid, market.market_id)
                self.notifier.send(
                    "error",
                    {"stage": "process", "market_id": market.market_id},
                    strategy_id=sid,
                )
        return stats

    def _process_market(self, strat: Strategy, market: Market, start: float,
                        executor: ExecutionClient, stats: CycleStats) -> None:
        cfg = strat.config
        sid = strat.strategy_id
        book = self._get_book(market.yes_token_id or "")
        snap = build_snapshot(cfg, market, Outcome.YES, book)
        if not is_tradeable(cfg, snap):
            return

        est = self._models[sid].estimate(market, Outcome.YES, snap, book)
        if est is None or not passes_thresholds(cfg, est):
            return

        action_outcome, action = (
            (Outcome.YES, "BUY YES") if est.edge >= 0 else (Outcome.NO, "BUY NO")
        )
        opp = Opportunity(market=market, outcome=action_outcome, pricing=snap,
                          estimate=est, suggested_action=action)
        self.ledger.record_opportunity(opp, strategy_id=sid)
        stats.opportunities += 1
        self.notifier.send("opportunity_detected",
                           self.notifier.opportunity_payload(opp), strategy_id=sid)
        log.info("[%s] opportunity: %s %s edge=%.3f conf=%.2f", sid, action,
                 market.question[:55], est.edge, est.confidence)

        # Per-strategy risk: state is scoped to THIS strategy's book only.
        state = RiskState(**self.ledger.risk_state_inputs(
            is_paper=executor.is_paper, starting_balance=start, strategy_id=sid))
        signal = self._risk[sid].evaluate(opp, state)
        self.ledger.record_signal(signal, strategy_id=sid)
        if not signal.accepted:
            stats.signals_rejected += 1
            self.notifier.send("risk_block",
                               self.notifier.signal_rejected_payload(signal), strategy_id=sid)
            return
        stats.signals_accepted += 1

        if cfg.mode == "analysis":
            return  # no orders, not even simulated
        self._execute_paper(strat, market, opp, signal, book, executor, stats)

    def _execute_paper(self, strat: Strategy, market, opp, signal, book,
                       executor: ExecutionClient, stats: CycleStats) -> None:
        sid = strat.strategy_id
        order = executor.place_limit_order(
            market_id=market.market_id,
            token_id=signal.token_id or "",
            outcome=opp.outcome,
            side=OrderSide.BUY,
            price=signal.target_price or (opp.pricing.best_ask or 0.5),
            notional_usd=signal.notional_usd or 0.0,
            reference_book=book,
        )
        self.ledger.record_order(order, strategy_id=sid)
        if order.status.value not in ("FILLED", "PARTIAL"):
            return
        self.ledger.record_fill(order, strategy_id=sid)
        self.ledger.open_position(market=market, order=order, strategy_id=sid)
        stats.paper_orders += 1
        event = "paper_order_opened" if order.is_paper else "live_order_created"
        self.notifier.send(event,
                           self.notifier.order_payload(order, market.question, market.url),
                           strategy_id=sid)

    # -- arbitrage (global, once per cycle) -----------------------------------
    def _scan_arbitrage(self, raw: list[Market], stats: CycleStats) -> None:
        top_n = int(self.cfg.get("engine", "scan_top_n_for_book", default=60))
        passing = [m for m in raw if filter_market(m, self.cfg).passed][:top_n]
        for market in passing:
            try:
                yes_book = self._get_book(market.yes_token_id or "")
                no_book = self._get_book(market.no_token_id or "")
                arb = detect_arbitrage(self.cfg, market, yes_book, no_book)
                if arb is None:
                    continue
                self.ledger.record_event("arbitrage_detected", arb.to_dict(),
                                         strategy_id="arbitrage")
                self.notifier.send("arbitrage_detected", {
                    "market": arb.question, "outcome": "YES+NO",
                    "market_price": arb.combined_cost, "edge": arb.net_edge,
                    "suggested_action": "ARBITRAGE BUY YES+NO",
                    "liquidity": arb.notional_usd, "url": arb.url,
                }, strategy_id="arbitrage")
                log.info("ARBITRAGE: %s combined=%.4f net_edge=%.4f $%.0f",
                         market.question[:50], arb.combined_cost, arb.net_edge,
                         arb.notional_usd)
            except Exception:
                log.exception("arbitrage scan failed for %s", market.market_id)

    # -- balance snapshot (per strategy) --------------------------------------
    def _snapshot_all(self) -> None:
        for strat in self.strategies:
            sid = strat.strategy_id
            executor = self._exec[sid]
            try:
                start = float(strat.config.get("paper", "starting_balance_usd", default=1000))
                inputs = self.ledger.risk_state_inputs(
                    is_paper=executor.is_paper, starting_balance=start, strategy_id=sid)
                blocked = inputs["total_exposure_usd"]
                realized = self.ledger.query(
                    "SELECT COALESCE(SUM(realized_pnl),0) p FROM positions "
                    "WHERE is_paper=? AND strategy_id=?",
                    (1 if executor.is_paper else 0, sid),
                )[0]["p"]
                self.ledger.snapshot_balance(
                    cash=start - blocked + float(realized),
                    blocked=blocked, unrealized=0.0, realized=float(realized),
                    is_paper=executor.is_paper, strategy_id=sid,
                )
            except Exception:
                log.exception("Balance snapshot failed for %s", sid)

    def close(self) -> None:
        self.ledger.close()
        for ex in self._exec.values():
            ex.close()
