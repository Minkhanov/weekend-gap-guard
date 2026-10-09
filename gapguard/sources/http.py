"""A polite HTTP client: one request per `min_interval` seconds, bounded retries, Retry-After.

Every data source goes through it, so the whole tool never hammers a public endpoint:
the default pace is 2 requests/s, far under the documented Binance Web3 limits
(5 RPS per endpoint, 1 200 requests/min per IP).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

log = logging.getLogger("gapguard.http")

RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A source could not deliver (network, HTTP status, or an API-level error code)."""


class PoliteClient:
    def __init__(
        self,
        *,
        min_interval: float = 0.5,
        retries: int = 3,
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval = min_interval
        self.retries = retries
        self._sleep = sleep
        self._clock = clock
        self._last = -1e9
        self.calls = 0
        self.last_latency_ms: float | None = None
        self._client = httpx.Client(timeout=timeout, headers=headers or {}, transport=transport,
                                    follow_redirects=False)

    def _pace(self) -> None:
        wait = self.min_interval - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            self._pace()
            started = time.perf_counter()
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if attempt >= self.retries:
                    raise SourceError(f"{method} {_short(url)}: {exc.__class__.__name__}: {exc}") from exc
                delay = min(2 ** attempt, 8)
                log.warning("network error on %s (%s), retry in %ss", _short(url), exc.__class__.__name__, delay)
                self._sleep(delay)
                attempt += 1
                continue
            finally:
                self.calls += 1
            self.last_latency_ms = (time.perf_counter() - started) * 1000
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                delay = _retry_after(resp) or min(2 ** attempt, 8)
                log.warning("HTTP %s on %s, retry in %ss", resp.status_code, _short(url), delay)
                self._sleep(delay)
                attempt += 1
                continue
            if resp.status_code != 200:
                raise SourceError(f"{method} {_short(url)}: HTTP {resp.status_code}: {resp.text[:160]!r}")
            return resp

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        resp = self.request("GET", url, params=params, **kwargs)
        return _json(resp, url)

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        resp = self.request("POST", url, json=payload, **kwargs)
        return _json(resp, url)

    def close(self) -> None:
        self._client.close()


def _json(resp: httpx.Response, url: str) -> Any:
    if not resp.content:
        # Seen live: AWS WAF answers 202 with an empty body to non-browser clients on some doc pages.
        raise SourceError(f"empty body from {_short(url)} (HTTP {resp.status_code})")
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"non-JSON body from {_short(url)}: {resp.text[:120]!r}") from exc


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), 60.0)
    except ValueError:
        return None


def _short(url: str) -> str:
    return url.split("?", 1)[0][-90:]
