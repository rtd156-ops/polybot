"""Minimal HTTP client with retries/backoff. No secrets, no auth headers here."""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

from ..logging_setup import get_logger

log = get_logger("polybot.http")


class HttpError(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff: float = 1.5,
        user_agent: str = "polybot/1.0",
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
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
