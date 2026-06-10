"""Live Polymarket CLOB execution -- DESIGNED, GATED OFF.

This is the ONLY module that can move real money, and it cannot run unless ALL
of the following hold (enforced in ``__init__`` and in config validation):

    * config mode == "live"
    * execution.live_enabled == true
    * full CLOB credentials present in the environment (.env)
    * the optional ``py-clob-client`` dependency is installed

V1 ships with this disabled. The method bodies sketch the real integration but
raise ``LiveExecutionDisabled`` until you deliberately wire and enable it on a
secured host. By design it uses LIMIT orders only -- no aggressive market orders.

SECURITY: secrets are read from the environment, held only in memory, and never
logged. Do not add print/log statements that include key material.
"""

from __future__ import annotations

from typing import Optional

from ..config import Config
from ..logging_setup import get_logger
from ..models import OrderBook, OrderResult, OrderSide, Outcome
from .base import ExecutionClient

log = get_logger("polybot.live")


class LiveExecutionDisabled(RuntimeError):
    """Raised whenever live execution is attempted while gated off."""


class PolymarketLiveExecution(ExecutionClient):
    is_paper = False

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Hard gate. Construction fails unless the system is fully cleared.
        if not cfg.live_truly_enabled:
            raise LiveExecutionDisabled(
                "Live execution is OFF. Requires mode=live AND "
                "execution.live_enabled=true AND CLOB credentials in .env. "
                "See deploy/DEPLOY.md before enabling."
            )
        self._client = self._build_client()
        self.max_notional = float(
            cfg.get("execution", "live_max_order_notional_usd", default=50)
        )
        log.warning("LIVE execution client constructed -- REAL ORDERS ENABLED")

    def _build_client(self):
        """Construct the authenticated CLOB client from env secrets.

        Imports are local so the optional dependency is only needed for live.
        """
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LiveExecutionDisabled(
                "py-clob-client is not installed. Install it on the live host "
                "(see requirements.txt) before enabling live execution."
            ) from exc

        s = self.cfg.secrets
        clob_url = self.cfg.get("data_sources", "clob_base_url")
        # NOTE: do not log any of these values.
        client = ClobClient(
            host=clob_url,
            key=s.poly_private_key,
            chain_id=137,  # Polygon mainnet
        )
        client.set_api_creds(
            client.create_or_derive_api_creds()
            if not s.poly_clob_api_key
            else {
                "api_key": s.poly_clob_api_key,
                "api_secret": s.poly_clob_api_secret,
                "api_passphrase": s.poly_clob_api_passphrase,
            }
        )
        return client

    # -- ExecutionClient interface -------------------------------------------
    def place_limit_order(
        self,
        *,
        market_id: str,
        token_id: str,
        outcome: Outcome,
        side: OrderSide,
        price: float,
        notional_usd: float,
        reference_book: Optional[OrderBook] = None,
    ) -> OrderResult:
        self._guard()
        if notional_usd > self.max_notional:
            raise ValueError(
                f"notional ${notional_usd} exceeds live cap ${self.max_notional}"
            )
        if self.cfg.get("execution", "live_order_type") != "limit":
            raise ValueError("live execution only supports limit orders by default")
        # Real implementation (kept inert until live is wired & tested):
        #   from py_clob_client.clob_types import OrderArgs
        #   size = notional_usd / price
        #   signed = self._client.create_order(OrderArgs(
        #       token_id=token_id, price=price, size=size,
        #       side=side.value,
        #   ))
        #   resp = self._client.post_order(signed)
        #   return self._to_order_result(resp, market_id, token_id, outcome, side)
        raise LiveExecutionDisabled(
            "place_limit_order: live trading is not wired in V1."
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        self._guard()
        # return self._client.cancel(order_id) -> normalized OrderResult
        raise LiveExecutionDisabled("cancel_order: live trading is not wired in V1.")

    def get_order_status(self, order_id: str) -> Optional[OrderResult]:
        self._guard()
        # return self._client.get_order(order_id) -> normalized OrderResult
        raise LiveExecutionDisabled("get_order_status: live trading is not wired in V1.")

    def reconcile(self, desired=None, open_orders=None):
        """Reconcile desired vs. open orders, returning the minimal action plan.

        The diff itself is pure (``execution.reconcile.reconcile``) and fully
        unit-tested. Actually fetching open orders from the CLOB and submitting
        the plan stays gated until live is wired & tested.
        """
        self._guard()
        from .reconcile import reconcile as _reconcile
        if desired is None or open_orders is None:
            raise LiveExecutionDisabled(
                "reconcile: fetching live open orders is not wired in V1."
            )
        return _reconcile(desired, open_orders)

    def _guard(self) -> None:
        if not self.cfg.live_truly_enabled:
            raise LiveExecutionDisabled("live gate closed at call time")
