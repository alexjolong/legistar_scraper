"""Polite fetching of web pages, and a bronze store that keeps them as fetched.

Sources without an API (the Revisor of Statutes, the county's ordinance pages) are
read page by page. Each page is stored unparsed under
``<data_dir>/bronze/<source>/<key>`` with a ``.meta.json`` beside it, so a parser fix
never needs another crawl, and so every edition ever fetched is kept.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .client import RETRY_STATUS, USER_AGENT, RateLimiter

log = logging.getLogger(__name__)


class Disallowed(RuntimeError):
    """The URL is under a path the site's robots.txt asks crawlers to leave alone."""


class Fetcher:
    """One request per second by default, retries with back-off, and a list of
    path prefixes that are never requested."""

    def __init__(
        self,
        *,
        requests_per_second: float = 1.0,
        disallow: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
        max_retries: int = 4,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.disallow = disallow
        self.max_retries = max_retries
        self.request_count = 0
        self._limiter = RateLimiter(requests_per_second)
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, url: str, params: Any = None) -> httpx.Response | None:
        """GET ``url``; ``None`` for a 404. Raises after the retries are used up.
        ``params`` is a dict, or a list of pairs when a name repeats."""
        path = urlsplit(url).path
        if any(path.startswith(prefix) for prefix in self.disallow):
            raise Disallowed(f"{url} is disallowed by the site's robots.txt")
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            self.request_count += 1
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                problem, retry_after = repr(exc), None
            else:
                if response.status_code == 404:
                    return None
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    return response
                problem, retry_after = f"HTTP {response.status_code}", response.headers.get("Retry-After")
            if attempt == self.max_retries:
                raise RuntimeError(f"GET {url} failed after {attempt + 1} attempts: {problem}")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2.0 ** (attempt + 1), 60.0)
            log.warning("GET %s -> %s; retrying in %.0fs", url, problem, delay)
            time.sleep(delay)
        raise AssertionError("unreachable")


class PageStore:
    """Raw files under ``<data_dir>/bronze/<source>/``, each with its metadata."""

    def __init__(self, data_dir: Path, source: str) -> None:
        self.root = Path(data_dir) / "bronze" / source

    def path(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def read(self, key: str) -> bytes | None:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def meta(self, key: str) -> dict[str, Any]:
        try:
            return json.loads(self.path(key + ".meta.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def age_days(self, key: str) -> float | None:
        """Days since the page was fetched; ``None`` if it never was."""
        fetched = self.meta(key).get("fetched_at_utc")
        if not fetched or not self.exists(key):
            return None
        return (datetime.now(UTC) - datetime.fromisoformat(fetched)).total_seconds() / 86400

    def write(self, key: str, content: bytes, **meta: Any) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, data in (
            (path, content),
            (
                self.path(key + ".meta.json"),
                json.dumps({"fetched_at_utc": datetime.now(UTC).isoformat(timespec="seconds"), **meta}).encode(),
            ),
        ):
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)

    def keys(self, prefix: str = "", suffix: str = ".html") -> list[str]:
        """Stored keys under ``prefix``, sorted, without the metadata files."""
        base = self.path(prefix)
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob(f"*{suffix}")
            if p.is_file() and not p.name.endswith((".meta.json", ".tmp"))
        )

    def save(self, key: str, fetcher: Fetcher, url: str, params: dict[str, Any] | None = None) -> bytes | None:
        """Fetch ``url`` into ``key``. A 404 is recorded, so it is not asked for again."""
        response = fetcher.get(url, params)
        if response is None:
            self.write(key + ".missing", b"", url=url, status=404)
            return None
        self.write(key, response.content, url=str(response.url), status=response.status_code)
        return response.content
