"""Thin, polite client for the Legistar Web API (https://webapi.legistar.com/Help).

The API is OData-flavoured: collections accept ``$top``/``$skip``/``$filter``/
``$orderby`` and return at most 1000 rows per request. Nothing here is specific
to Hennepin County; any Legistar tenant works by changing ``client``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://webapi.legistar.com/v1"
PAGE_SIZE = 1000  # server-side maximum
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "legistar-scraper/0.1 (civic research; httpx)"


class RateLimiter:
    """Spaces calls evenly across threads: at most ``per_second`` starts per second."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        if slot > now:
            time.sleep(slot - now)


class LegistarClient:
    """Rate-limited, retrying GET client. Safe to share across threads."""

    def __init__(
        self,
        client: str = "hennepinmn",
        *,
        requests_per_second: float = 5.0,
        max_retries: int = 5,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client = client
        self.max_retries = max_retries
        self.request_count = 0
        self._limiter = RateLimiter(requests_per_second)
        self._http = httpx.Client(
            base_url=f"{BASE_URL}/{client}/",
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LegistarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return parsed JSON, or ``None`` if the resource is a 404."""
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            self.request_count += 1
            try:
                response = self._http.get(path.lstrip("/"), params=params)
            except httpx.TransportError as exc:
                problem: str = repr(exc)
                retry_after = None
            else:
                if response.status_code == 404:
                    return None
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    return response.json()
                problem = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After")

            if attempt == self.max_retries:
                raise RuntimeError(f"GET {path} failed after {attempt + 1} attempts: {problem}")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2.0**attempt, 60.0)
            log.warning("GET %s -> %s; retrying in %.0fs", path, problem, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def get_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        order_by: str | None = None,
        page_size: int = PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Yield every row of a collection, paging with ``$top``/``$skip``.

        Pass ``order_by`` (normally the primary key) for any collection that can
        exceed one page: without a stable order, rows can repeat or go missing
        between pages.
        """
        skip = 0
        while True:
            page_params = dict(params or {})
            page_params["$top"] = page_size
            page_params["$skip"] = skip
            if order_by:
                page_params["$orderby"] = order_by
            page = self.get(path, page_params) or []
            yield from page
            if len(page) < page_size:
                return
            skip += page_size
