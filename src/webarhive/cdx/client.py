"""CDX Server API client (spec §3.1).

Fetches the index of all captures for a domain without downloading
pages. Supports matchType=domain (default, captures the whole tree
including www and subdomains and inner pages) or matchType=host (only
that hostname — used when CHECK_SUBDOMAINS is on, §2.4).

Primary dedup is server-side via collapse=urlkey+digest: identical
content under the same URL is collapsed; different URLs stay. Further
version-level collapsing for topic analysis happens in analysis/.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from webarhive.cdx.throttle import IAThrottle

logger = logging.getLogger(__name__)

CDX_URL = "http://web.archive.org/cdx/search/cdx"
DEFAULT_FIELDS = ("urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length")
PAGE_LIMIT = 5000  # rows per page; resumeKey paginates further


@dataclass(frozen=True, slots=True)
class CdxRow:
    urlkey: str
    timestamp: str  # YYYYMMDDhhmmss
    original: str  # full URL
    mimetype: str
    statuscode: str  # may be empty string in CDX
    digest: str
    length: str

    @classmethod
    def from_list(cls, row: list[str]) -> CdxRow:
        return cls(
            urlkey=row[0],
            timestamp=row[1],
            original=row[2],
            mimetype=row[3],
            statuscode=row[4],
            digest=row[5],
            length=row[6] if len(row) > 6 else "",
        )

    @property
    def status_bucket(self) -> Literal["200", "3xx", "404", "5xx", "other"]:
        sc = self.statuscode
        if not sc or not sc.isdigit():
            return "other"
        code = int(sc)
        if code == 200:
            return "200"
        if 300 <= code < 400:
            return "3xx"
        if code == 404:
            return "404"
        if 500 <= code < 600:
            return "5xx"
        return "other"


class CdxClient:
    """Async client for CDX Server API with shared IA throttle + backoff."""

    def __init__(
        self,
        *,
        throttle: IAThrottle,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        timeout: float = 60.0,
    ) -> None:
        self._throttle = throttle
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "webarhive-checker/0.1 (+internal)"},
        )
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CdxClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _get(self, params: list[tuple[str, str]] | dict) -> httpx.Response:
        await self._throttle.acquire()

        # Retry only on transient failures: connection errors, timeouts,
        # 429 (throttle), 5xx (server). NOT on 4xx — those are permanent
        # and retrying them just adds 60s of dead waiting per request.
        def _is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout,
                                httpx.WriteTimeout, httpx.PoolTimeout,
                                httpx.ConnectTimeout, asyncio.TimeoutError)):
                return True
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                return code == 429 or 500 <= code < 600
            return False

        retryer = AsyncRetrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=self._backoff_base, min=self._backoff_base, max=60),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        async for attempt in retryer:
            with attempt:
                resp = await self._client.get(CDX_URL, params=params)
                # 429/5xx → raise a retryable error so tenacity backs off
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    url_for_log = (
                        dict(params).get("url") if isinstance(params, list) else params.get("url")
                    )
                    logger.warning(
                        "CDX throttled/failed status=%s for url=%s — backing off",
                        resp.status_code,
                        url_for_log,
                    )
                    raise httpx.HTTPStatusError(
                        "retryable", request=resp.request, response=resp
                    )
                # 4xx → permanent. raise_for_status fires a non-retryable
                # HTTPStatusError that propagates up the call chain.
                resp.raise_for_status()
                return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    async def iter_captures(
        self,
        domain: str,
        *,
        match_type: Literal["domain", "host"] = "domain",
        fields: tuple[str, ...] = DEFAULT_FIELDS,
        page_limit: int = PAGE_LIMIT,
        filters: tuple[str, ...] = (),
    ) -> AsyncIterator[CdxRow]:
        """Stream CDX rows for a domain, paginating via resumeKey.

        Server-side dedup uses BOTH collapse=urlkey and collapse=digest
        per spec §3.1: combined they collapse "same URL with same content"
        within each urlkey group while still preserving distinct URLs.
        httpx serialises a list-of-tuples into a repeated query parameter.

        `filters` — additional CDX server-side filters, e.g.
        `("statuscode:200",)` to only get successful captures, or
        `("statuscode:3..",)` for redirects. Server-side filtering cuts
        wire payload significantly on large archives.

        `gzip=true` is set unconditionally — CDX JSON for big domains is
        1-2 MB raw, ~5-10× smaller compressed. httpx auto-decompresses.
        """
        # base params dict (single-valued), then append the two collapse
        # parameters separately so they appear twice in the query string.
        base_params: list[tuple[str, str]] = [
            ("url", domain),
            ("matchType", match_type),
            ("output", "json"),
            ("fl", ",".join(fields)),
            ("collapse", "urlkey"),
            ("collapse", "digest"),
            ("limit", str(page_limit)),
            ("showResumeKey", "true"),
            ("gzip", "true"),
        ]
        for f in filters:
            base_params.append(("filter", f))
        resume_key: str | None = None
        page_no = 0

        while True:
            params = list(base_params)
            if resume_key:
                params.append(("resumeKey", resume_key))
            resp = await self._get(params)
            data = resp.json() if resp.content else []
            if not data:
                return

            # First row of first page is the header. Subsequent pages
            # don't repeat the header when resumeKey is used.
            rows = data
            if page_no == 0:
                rows = data[1:]  # drop header
            page_no += 1

            # Find resume key. CDX appends it as:
            # [..., [], ["resume_key_value"]] at the end of the page.
            next_resume: str | None = None
            cleaned: list[list[str]] = []
            for row in rows:
                if not row:
                    continue
                if len(row) == 1:
                    next_resume = row[0]
                    continue
                cleaned.append(row)

            for row in cleaned:
                try:
                    yield CdxRow.from_list(row)
                except (IndexError, TypeError):
                    logger.debug("malformed CDX row skipped: %r", row)

            if not next_resume:
                return
            resume_key = next_resume

    async def fetch_all(
        self,
        domain: str,
        *,
        match_type: Literal["domain", "host"] = "domain",
        filters: tuple[str, ...] = (),
    ) -> list[CdxRow]:
        return [
            r async for r in self.iter_captures(
                domain, match_type=match_type, filters=filters,
            )
        ]
