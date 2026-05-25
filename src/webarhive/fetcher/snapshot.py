"""Snapshot fetcher (spec §3.2).

Two URL flavours:
- machine parsing: `http://web.archive.org/web/<ts>id_/<url>` — raw HTML
  without the archive toolbar/scripts. `id_` is essential, otherwise the
  parser will read injected wayback markup as the page content.
- human snapshot: `https://web.archive.org/web/<ts>/<url>` — without `id_`,
  shows the toolbar so an operator can browse neighbouring captures.

Uses the same IA throttle as CDX.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from webarhive.cdx.throttle import IAThrottle

logger = logging.getLogger(__name__)

WAYBACK_BASE = "http://web.archive.org/web"
WAYBACK_HUMAN_BASE = "https://web.archive.org/web"


def snapshot_url(timestamp: str, original_url: str, *, for_human: bool = False) -> str:
    """Build the Wayback URL. `for_human` drops the `id_` suffix (spec §3.2)."""
    if for_human:
        return f"{WAYBACK_HUMAN_BASE}/{timestamp}/{original_url}"
    return f"{WAYBACK_BASE}/{timestamp}id_/{original_url}"


@dataclass(frozen=True, slots=True)
class SnapshotContent:
    url: str             # the wayback URL actually fetched (with id_)
    final_url: str       # after redirects, if any
    status_code: int
    headers: dict[str, str]
    body: bytes
    encoding: str | None  # detected; None means default utf-8


def _detect_encoding(body: bytes, content_type: str | None) -> str | None:
    """Best-effort encoding detection. Old snapshots are often
    windows-1251 / koi8-r — utf-8 alone would mangle Cyrillic."""
    # 1) Trust Content-Type charset if explicit.
    if content_type and "charset=" in content_type.lower():
        try:
            return content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
        except Exception:
            pass

    # 2) Meta tag inside the head.
    head = body[:4096].lower()
    for marker in (b"charset=", b"charset = "):
        idx = head.find(marker)
        if idx != -1:
            tail = head[idx + len(marker) :]
            end = 0
            for sep in (b'"', b"'", b" ", b">", b"/", b";"):
                p = tail.find(sep)
                if p != -1 and (end == 0 or p < end):
                    end = p
            if end > 0:
                enc = tail[:end].decode("ascii", errors="ignore").strip()
                if enc:
                    return enc

    # 3) Fallback to charset-normalizer.
    try:
        from charset_normalizer import from_bytes
        result = from_bytes(body).best()
        if result is not None:
            return result.encoding
    except Exception:
        pass
    return None


class SnapshotFetcher:
    def __init__(
        self,
        *,
        throttle: IAThrottle,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        timeout: float = 30.0,
    ) -> None:
        self._throttle = throttle
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # we WANT to see 3xx Location
            headers={"User-Agent": "webarhive-checker/0.1 (+internal)"},
        )
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> SnapshotFetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def fetch(self, timestamp: str, original_url: str) -> SnapshotContent:
        url = snapshot_url(timestamp, original_url, for_human=False)
        await self._throttle.acquire()

        # Same retry policy as CDX client: 429/5xx and connection errors
        # only. A 404 on a snapshot means the capture genuinely doesn't
        # exist — don't waste ~60s retrying it.
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
                resp = await self._client.get(url)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    logger.warning("snapshot retryable status=%s url=%s", resp.status_code, url)
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                # Don't raise_for_status() — 4xx (e.g. snapshot not found)
                # is information we want to surface, not retry. The caller
                # already handles non-200 bodies via fetcher try/except in
                # analysis layers (_fetch_light, _fetch_heavy).
                content_type = resp.headers.get("content-type")
                enc = _detect_encoding(resp.content, content_type)
                return SnapshotContent(
                    url=url,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    headers=dict(resp.headers),
                    body=resp.content,
                    encoding=enc,
                )
        raise RuntimeError("unreachable")  # pragma: no cover
