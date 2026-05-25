"""WhoisJSON client (spec extension §11, §13).

GET https://whoisjson.com/api/v1/whois?domain=<domain>
Authorization: TOKEN=<API_KEY>

Used to fetch the real registration date of a domain (not the
"first archive activity" age proxy). Free plan:
  - 1000 requests / month
  - 20 requests / minute
  - server returns `Remaining-Requests` header → we use it to bail out
    BEFORE hitting 429, leaving headroom for other runs in the month.

Heavy caching is essential — see WhoisCache table.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

WHOISJSON_URL = "https://whoisjson.com/api/v1/whois"

Status = Literal["got", "from_cache", "limit", "error", "disabled", "not_configured"]


@dataclass(frozen=True, slots=True)
class WhoisResult:
    domain: str
    registration_date: datetime | None
    status: Status
    error: str | None = None
    remaining_requests: int | None = None


def _parse_date(raw) -> datetime | None:
    """WhoisJSON normalises to ISO-like; we accept the common shapes."""
    if not raw:
        return None
    if isinstance(raw, dict):
        # Some RDAP-style responses wrap the date in an object.
        raw = raw.get("date") or raw.get("value") or raw.get("registered")
    if not raw:
        return None
    s = str(raw).strip()
    # Stripping a stray timezone designator that fromisoformat can't parse.
    s = s.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt is None:
                # fromisoformat handles +00:00 timezone since 3.11
                return datetime.fromisoformat(s).replace(tzinfo=None)
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _extract_registration_date(payload: dict) -> datetime | None:
    """Try the common WhoisJSON / RDAP field names in order."""
    candidates = (
        "created", "creation_date", "created_date",
        "registration_date", "registered",
        "registry_creation_date",
    )
    for key in candidates:
        if key in payload:
            d = _parse_date(payload[key])
            if d:
                return d
    # nested rdap structure
    if isinstance(payload.get("events"), list):
        for ev in payload["events"]:
            if isinstance(ev, dict) and ev.get("eventAction") in (
                "registration", "created"
            ):
                d = _parse_date(ev.get("eventDate"))
                if d:
                    return d
    return None


class WhoisClient:
    """Throttled WhoisJSON client. Stops calling the API when the
    server-reported Remaining-Requests drops below `monthly_floor`."""

    def __init__(
        self,
        *,
        api_key: str,
        rate_limit: float = 20.0 / 60.0,  # req/sec
        monthly_floor: int = 10,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._min_interval = (1.0 / rate_limit) if rate_limit > 0 else 0
        self._monthly_floor = monthly_floor
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()
        # Cached "Remaining-Requests" from last successful response.
        # Once it drops below the floor, we stop calling for the run.
        self.remaining: int | None = None
        self.exhausted: bool = False
        self.fatal_config_error: bool = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "WhoisClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def lookup(self, domain: str) -> WhoisResult:
        if not self._api_key:
            return WhoisResult(domain, None, "not_configured")
        if self.fatal_config_error:
            return WhoisResult(domain, None, "error", error="ключ невалиден",
                               remaining_requests=self.remaining)
        if self.exhausted:
            return WhoisResult(domain, None, "limit",
                               error="месячный лимит почти исчерпан",
                               remaining_requests=self.remaining)

        # Pace requests across the whole client. Single shared gate.
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed_at = time.monotonic() + self._min_interval

        try:
            resp = await self._client.get(
                WHOISJSON_URL,
                params={"domain": domain},
                headers={"Authorization": f"TOKEN={self._api_key}"},
            )
        except httpx.HTTPError as exc:
            return WhoisResult(domain, None, "error",
                               error=f"сеть: {type(exc).__name__}",
                               remaining_requests=self.remaining)

        # Remaining-Requests header (lowercased by httpx). May be absent.
        rem_hdr = resp.headers.get("remaining-requests") or resp.headers.get("Remaining-Requests")
        if rem_hdr is not None:
            try:
                self.remaining = int(rem_hdr)
                if self.remaining <= self._monthly_floor:
                    self.exhausted = True
            except ValueError:
                pass

        if resp.status_code == 401:
            self.fatal_config_error = True
            return WhoisResult(domain, None, "error",
                               error="WHOIS: 401 невалидный ключ",
                               remaining_requests=self.remaining)
        if resp.status_code == 403:
            self.fatal_config_error = True
            return WhoisResult(domain, None, "error",
                               error="WHOIS: 403 — подтвердите email в whoisjson",
                               remaining_requests=self.remaining)
        if resp.status_code == 429:
            self.exhausted = True
            return WhoisResult(domain, None, "limit",
                               error="WHOIS: 429 лимит",
                               remaining_requests=self.remaining)
        if resp.status_code >= 400:
            return WhoisResult(domain, None, "error",
                               error=f"WHOIS: HTTP {resp.status_code}",
                               remaining_requests=self.remaining)

        try:
            payload = resp.json()
        except Exception:
            return WhoisResult(domain, None, "error",
                               error="WHOIS: невалидный JSON",
                               remaining_requests=self.remaining)

        if not isinstance(payload, dict):
            return WhoisResult(domain, None, "error",
                               error="WHOIS: ответ не объект",
                               remaining_requests=self.remaining)

        date = _extract_registration_date(payload)
        if date is None:
            return WhoisResult(domain, None, "error",
                               error="WHOIS: не нашли дату регистрации",
                               remaining_requests=self.remaining)
        return WhoisResult(domain, date, "got",
                           remaining_requests=self.remaining)
