"""Minimal HTTP client with retries/backoff + rate limiting.

No secrets and no auth headers here -- this is the public data path only.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from ..logging_setup import get_logger

log = get_logger("polybot.http")


class HttpError(RuntimeError):
    pass


class TokenBucket:
    """Thread-safe token bucket: smooths request rate to respect API limits.

    Refills at ``rate`` tokens/sec up to ``capacity``. ``acquire`` blocks just
    long enough for a token to be available, so callers never exceed the rate.
    """

    def __init__(self, rate_per_sec: float, capacity: Optional[float] = None) -> None:
        self.rate = max(0.0, float(rate_per_sec))
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_sec))
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        if self.rate <= 0:  # disabled => no limiting
            return
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                time.sleep(deficit / self.rate)


class HttpClient:
    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff: float = 1.5,
        user_agent: str = "polybot/1.0",
        rate_limit_per_sec: float = 0.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._bucket = TokenBucket(rate_limit_per_sec) if rate_limit_per_sec > 0 else None
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )

    def get_json(
        self, url: str, params: Optional[dict] = None
    ) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if self._bucket is not None:
                    self._bucket.acquire()
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code >= 500:
                    raise HttpError(f"{resp.status_code} from {url}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, HttpError, ValueError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    sleep_for = self.backoff * (2 ** (attempt - 1))
                    log.warning(
                        "GET attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt, self.max_retries, type(exc).__name__, sleep_for,
                    )
                    time.sleep(sleep_for)
        raise HttpError(f"GET {url} failed after {self.max_retries} attempts: {last_exc}")

    def close(self) -> None:
        self._session.close()
