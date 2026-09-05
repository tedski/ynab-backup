"""YNAB REST API client with retry, backoff, and adaptive rate limiting."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import requests

from ynab_backup.constants import DEFAULT_API_BASE_URL, DEFAULT_TIMEOUT
from ynab_backup.exceptions import YnabError

LOG = logging.getLogger("ynab-backup")


class SlidingWindowRateLimiter:
    """Ensures no more than ``max_per_hour`` API requests per rolling hour.

    Before each call, ``acquire()`` checks the sliding window and blocks if
    the limit would be exceeded.  After a 429 response, ``report_429()``
    inserts a synthetic timestamp to penalize the window so the rate adjusts
    down without requiring another 429.
    """

    def __init__(
        self,
        max_per_hour: int,
        _get_time: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            max_per_hour: Maximum requests allowed per rolling hour.
                Set to 0 or negative to disable limiting.
            _get_time: Time function for testing (defaults to ``time.time``).
        """
        self.max_per_hour = max_per_hour
        self._timestamps: list[float] = []
        self._time = _get_time or time.time

    def acquire(self) -> None:
        """Block until a request slot is available, then record it."""
        if self.max_per_hour <= 0:
            return
        now = self._time()
        cutoff = now - 3600
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        while len(self._timestamps) >= self.max_per_hour:
            wait = self._timestamps[0] + 3600 - now
            if wait <= 0:
                break
            LOG.warning("rate limit window full; sleeping %.0fs", wait)
            time.sleep(wait)
            now = self._time()
            cutoff = now - 3600
            self._timestamps = [t for t in self._timestamps if t > cutoff]
        self._timestamps.append(self._time())

    def report_429(self, retry_after: int | None = None) -> None:
        """Penalize the window after a server-side 429.

        Inserts a synthetic timestamp that ages out in ``retry_after``
        seconds (or 60 if not provided).  This reduces available quota
        for the penalty duration without needing to encounter another 429.
        """
        delay = min(retry_after, 300) if retry_after else 60
        self._timestamps.append(self._time() - 3600 + delay)


class YnabClientProtocol(Protocol):
    """Structural interface for any client that talks to the YNAB API.

    Both the real ``YnabClient`` and test ``FakeClient`` conform to this
    protocol. Using a Protocol (not an ABC) keeps it lightweight — no
    inheritance required, no runtime metaclass machinery — while giving
    the type checker and IDE the contract they need.
    """

    def get_list(self, path: str, key: str, **params: Any) -> list[dict]:
        """GET a list resource with pagination."""

    def get(self, path: str, **params: Any) -> dict:
        """GET a single resource."""

    def post(self, path: str, payload: dict) -> dict:
        """POST a resource."""

    def patch(self, path: str, payload: dict) -> dict:
        """PATCH a resource."""

    def delete(self, path: str) -> dict:
        """DELETE a resource."""


class YnabClient:
    """Thin wrapper over the YNAB REST API with retry/backoff and adaptive rate limiting."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        max_requests_per_hour: int = 200,
    ) -> None:
        """Initialize the YNAB API client.

        Args:
            token: YNAB API bearer token.
            base_url: API base URL.
            timeout: Request timeout in seconds.
            max_requests_per_hour: Maximum API requests per rolling hour.
                Set to 0 to disable rate limiting.
        """
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        self.timeout = timeout
        self._rate_limiter = SlidingWindowRateLimiter(max_requests_per_hour)

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(2**attempt, 30)
        LOG.warning("retrying in %ds (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        self._rate_limiter.acquire()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(4):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise YnabError(f"request failed for {url}: {exc}") from exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 3:
                    raise YnabError(f"server error {resp.status_code} for {url}: {resp.text}")
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        retry_secs = min(int(retry_after), 300) if retry_after else 60
                    except ValueError:
                        retry_secs = 30
                    LOG.warning("rate limited; waiting %ds (Retry-After)", retry_secs)
                    time.sleep(retry_secs)
                    self._rate_limiter.report_429(retry_after=retry_secs)
                    continue
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 404:
                raise YnabError(f"not found: {url}")
            if resp.status_code == 409:
                raise YnabError(f"conflict (409) for {url}: {resp.text}")
            if resp.status_code >= 400:
                raise YnabError(f"{resp.status_code} for {url}: {resp.text}")
            return resp.json().get("data", {})
        raise YnabError(f"exhausted retries for {url}")

    def get_list(
        self, path: str, key: str, *, page_size: int | None = None, **params: Any
    ) -> list[dict]:
        """GET a list resource, transparently following pagination.

        Args:
            path: API path (e.g. ``/budgets/{id}/transactions``).
            key: Top-level data key to extract from each response page.
            page_size: Optional page size for the first request.
            **params: Additional query parameters.

        Returns:
            Full list of resource dicts across all pages.
        """
        results: list[dict] = []
        call_params = dict(params)
        if page_size is not None:
            call_params["page_size"] = page_size
        next_path: str | None = path
        while next_path is not None:
            if next_path != path and next_path.startswith("http"):
                data = self._request("GET", next_path)
            else:
                data = self._request("GET", next_path, params=call_params or None)
            results.extend(data.get(key, []))
            pagination = data.get("pagination")
            next_path = pagination.get("next") if pagination else None
            call_params = {}
        return results

    def get(self, path: str, **params: Any) -> dict:
        """GET a single resource.

        Args:
            path: API path.
            **params: Query parameters.

        Returns:
            Response data dict.
        """
        return self._request("GET", path, params=params or None)

    def post(self, path: str, payload: dict) -> dict:
        """POST a resource.

        Args:
            path: API path.
            payload: Request body.

        Returns:
            Response data dict.
        """
        return self._request("POST", path, json=payload)

    def patch(self, path: str, payload: dict) -> dict:
        """PATCH a resource.

        Args:
            path: API path.
            payload: Request body.

        Returns:
            Response data dict.
        """
        return self._request("PATCH", path, json=payload)

    def delete(self, path: str) -> dict:
        """DELETE a resource.

        Args:
            path: API path.

        Returns:
            Response data dict.
        """
        return self._request("DELETE", path)
