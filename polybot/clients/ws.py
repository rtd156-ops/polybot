"""CLOB WebSocket order-book client (opt-in, for low-latency strategies).

REST polling is the default and is fine for hour/day-horizon liquid markets.
For short-horizon strategies (e.g. crypto 5/15-min markets) you want a streamed
book. This client streams the CLOB market channel and rebuilds OrderBooks, with
automatic reconnection + exponential backoff.

The ``websocket-client`` dependency is optional and imported lazily, so nothing
here is needed for V1 analysis/paper. The message-parsing logic is split out
(``parse_book_message``) so it can be unit-tested without any network.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from ..logging_setup import get_logger
from ..models import BookLevel, OrderBook

log = get_logger("polybot.ws")

DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def parse_book_message(payload: dict) -> Optional[OrderBook]:
    """Parse a CLOB 'book' message into a normalized OrderBook. Pure function."""
    if not isinstance(payload, dict):
        return None
    if payload.get("event_type") not in (None, "book"):
        return None
    token_id = str(payload.get("asset_id") or payload.get("token_id") or "")
    if not token_id:
        return None

    def levels(key: str) -> list[BookLevel]:
        out: list[BookLevel] = []
        for entry in payload.get(key, []) or []:
            try:
                price, size = float(entry["price"]), float(entry["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                out.append(BookLevel(price=price, size=size))
        return out

    bids = sorted(levels("bids"), key=lambda l: l.price, reverse=True)
    asks = sorted(levels("asks"), key=lambda l: l.price)
    if not bids and not asks:
        return None
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


class ClobWebSocketClient:
    """Streams order books for a set of token ids, with auto-reconnect.

    Usage:
        ws = ClobWebSocketClient(token_ids, on_book=lambda b: ...)
        ws.start()   # background thread
        ...
        ws.stop()
    """

    def __init__(
        self,
        token_ids: list[str],
        on_book: Callable[[OrderBook], None],
        url: str = DEFAULT_WS_URL,
        max_backoff: float = 30.0,
    ) -> None:
        self.token_ids = token_ids
        self.on_book = on_book
        self.url = url
        self.max_backoff = max_backoff
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="polybot-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            import websocket  # type: ignore  (websocket-client, optional)
        except ImportError:
            log.error("websocket-client not installed; WS streaming unavailable.")
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=15)
                ws.send(json.dumps({"type": "market", "assets_ids": self.token_ids}))
                log.info("WS connected, subscribed to %d tokens", len(self.token_ids))
                backoff = 1.0  # reset on a healthy connection
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        break
                    self._dispatch(raw)
                ws.close()
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                if self._stop.is_set():
                    break
                log.warning("WS error (%s); reconnecting in %.1fs", type(exc).__name__, backoff)
                time.sleep(backoff)
                backoff = min(self.max_backoff, backoff * 2)

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        messages = data if isinstance(data, list) else [data]
        for msg in messages:
            book = parse_book_message(msg)
            if book is not None:
                try:
                    self.on_book(book)
                except Exception:
                    log.exception("on_book callback failed")
