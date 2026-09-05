"""YNAB REST API client with retry, backoff, and throttling."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests

from ynab_backup.constants import DEFAULT_API_BASE_URL, DEFAULT_TIMEOUT
from ynab_backup.exceptions import YnabError

LOG = logging.getLogger("ynab-backup")


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
    """Thin wrapper over the YNAB REST API with retry/backoff and throttling."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        throttle_seconds: float = 0,
    ) -> None:
        """Initialize the YNAB API client.

        Args:
            token: YNAB API bearer token.
            base_url: API base URL.
            timeout: Request timeout in seconds.
            throttle_seconds: Seconds to sleep between API calls.
        """
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        self.timeout = timeout
        self.throttle_seconds = throttle_seconds

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(2**attempt, 30)
        LOG.warning("retrying in %ds (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    def _throttle(self) -> None:
        if self.throttle_seconds > 0:
            time.sleep(self.throttle_seconds)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
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
                    if retry_after:
                        try:
                            delay = min(int(retry_after), 300)
                        except ValueError:
                            delay = 30
                        LOG.warning("rate limited; waiting %ds (Retry-After)", delay)
                        time.sleep(delay)
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
            self._throttle()
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
        self._throttle()
        return self._request("POST", path, json=payload)

    def patch(self, path: str, payload: dict) -> dict:
        """PATCH a resource.

        Args:
            path: API path.
            payload: Request body.

        Returns:
            Response data dict.
        """
        self._throttle()
        return self._request("PATCH", path, json=payload)

    def delete(self, path: str) -> dict:
        """DELETE a resource.

        Args:
            path: API path.

        Returns:
            Response data dict.
        """
        self._throttle()
        return self._request("DELETE", path)
