"""Best-snapshot analyzer (spec extension).

For each topic epoch, pick the most complete archive snapshot of the
domain's HOME PAGE. "Most complete" = resource coverage (CSS/JS/img),
HTML integrity (length not absurdly small, body present), and time
consistency (resources archived near the page timestamp).

Resource availability is checked through the Internet Archive
Availability API — a fast point-query endpoint
(`https://archive.org/wayback/available?url=X&timestamp=Y`) that
answers "is this URL archived near this timestamp" in one call.
Earlier we used CDX with matchType=host per resource, which dumped
millions of paginated rows for CDN hosts and burned 15+ minutes per
domain. Availability API returns a tiny JSON immediately.

No LLM by default. Optional content_llm step (off by default) only
sanity-checks "is this a real epoch page vs coming-soon / 404".
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.parser import HTMLParser

from webarhive.cdx.client import CdxClient, CdxRow
from webarhive.cdx.throttle import IAThrottle
from webarhive.fetcher.snapshot import SnapshotFetcher, snapshot_url

logger = logging.getLogger(__name__)


HOME_PATH_RE = re.compile(r"^/?(index\.(html?|php|asp|aspx))?$", re.IGNORECASE)


def _is_home_page(url: str, source_domain: str) -> bool:
    """Return True if `url` looks like the domain's home page (root)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host != source_domain.lower():
        return False
    path = parts.path or "/"
    # accept "/", "/index.html", etc. — but not "/about", "/page/x"
    if HOME_PATH_RE.match(path) is None:
        return False
    # ignore query/fragment differences — they don't make it "not home"
    return True


@dataclass
class BestSnapshot:
    epoch_idx: int
    snapshot_url_human: str  # https://web.archive.org/web/<ts>/<original>
    timestamp: str
    score: float
    integrity: str  # "ok" / "truncated"
    resources_total: int = 0
    resources_archived: int = 0
    by_type: dict[str, tuple[int, int]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def _classify_resource(url: str) -> str:
    """Bucket a resource URL by type: css/js/img/font/other."""
    u = url.lower().split("?", 1)[0].split("#", 1)[0]
    if u.endswith((".css",)):
        return "css"
    if u.endswith((".js", ".mjs")):
        return "js"
    if u.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif")):
        return "img"
    if u.endswith((".woff", ".woff2", ".ttf", ".otf", ".eot")):
        return "font"
    return "other"


def _extract_resources(html_bytes: bytes, encoding: str | None, base: str) -> list[str]:
    """Pull external resource URLs from the page. Returns absolute URLs
    (resolved against `base`). Inline data: and javascript: scheme are
    skipped."""
    if not html_bytes:
        return []
    try:
        text = html_bytes.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        text = html_bytes.decode("utf-8", errors="replace")
    tree = HTMLParser(text)
    seen: set[str] = set()
    out: list[str] = []

    def add(u: str | None) -> None:
        if not u:
            return
        u = u.strip()
        if not u or u.startswith(("data:", "javascript:", "#", "mailto:")):
            return
        try:
            absu = urljoin(base, u)
        except Exception:
            return
        if absu in seen:
            return
        # Skip the page itself.
        if absu.split("#", 1)[0] == base.split("#", 1)[0]:
            return
        seen.add(absu)
        out.append(absu)

    for el in tree.css("link[rel*=stylesheet][href]"):
        add(el.attributes.get("href"))
    for el in tree.css("script[src]"):
        add(el.attributes.get("src"))
    for el in tree.css("img[src]"):
        add(el.attributes.get("src"))
    for el in tree.css("link[rel*=icon][href]"):
        add(el.attributes.get("href"))
    for el in tree.css("source[src]"):
        add(el.attributes.get("src"))
    # CSS @font-face / url(...) — light pass: anything in style[src]
    return out


def _select_candidates(
    rows: Sequence[CdxRow], top_n: int,
) -> list[CdxRow]:
    """Pick up to `top_n` evenly-spread candidates from a chronologically
    sorted list. Always include first + last to capture both the early
    epoch state and the latest."""
    if len(rows) <= top_n:
        return list(rows)
    if top_n <= 0:
        return []
    if top_n == 1:
        return [rows[len(rows) // 2]]
    step = (len(rows) - 1) / (top_n - 1)
    picked = []
    seen_idx: set[int] = set()
    for i in range(top_n):
        idx = int(round(i * step))
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        picked.append(rows[idx])
    return picked


def _ts_to_dt(ts: str) -> datetime | None:
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


AVAILABILITY_URL = "https://archive.org/wayback/available"


async def _check_resource(
    http: httpx.AsyncClient,
    throttle: IAThrottle,
    target_url: str,
    near_ts: str,
) -> bool:
    """True if IA's Availability API reports an archived snapshot of
    `target_url` near `near_ts`. One tiny JSON request per resource —
    way cheaper than dumping the whole CDX index for the host."""
    if not target_url:
        return False
    try:
        await throttle.acquire()
        resp = await http.get(
            AVAILABILITY_URL,
            params={"url": target_url, "timestamp": near_ts},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
    except Exception as exc:
        logger.debug("availability check failed for %s: %s", target_url, exc)
        return False
    snap = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available"):
        return False
    status = str(snap.get("status") or "")
    return status.startswith("2") or status.startswith("3")


async def best_snapshot_for_epoch(
    *,
    epoch_idx: int,
    candidates: Sequence[CdxRow],
    source_domain: str,
    fetcher: SnapshotFetcher,
    cdx: CdxClient,
    http: httpx.AsyncClient,
    throttle: IAThrottle,
    top_n: int = 3,
    max_resources_per_candidate: int = 8,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> BestSnapshot | None:
    """Pick the best home-page snapshot from the epoch's candidate list.

    candidates = 200-status CDX rows that look like the home page,
    already filtered + sorted chronologically.
    """
    if not candidates:
        return None

    chosen = _select_candidates(candidates, top_n)
    best: BestSnapshot | None = None

    async def _emit(msg: str) -> None:
        if progress is not None:
            try:
                await progress(msg)
            except Exception:
                pass

    for cand_idx, row in enumerate(chosen, start=1):
        await _emit(
            f"эпоха #{epoch_idx+1}: кандидат {cand_idx}/{len(chosen)} "
            f"({row.timestamp[:8]}) — забираю HTML"
        )
        try:
            content = await fetcher.fetch(row.timestamp, row.original)
        except Exception as exc:
            logger.warning("best-snap fetch failed for %s: %s", row.original, exc)
            continue
        if not content.body:
            continue

        body_len = len(content.body)
        integrity = "ok" if body_len >= 1024 else "truncated"
        resources = _extract_resources(content.body, content.encoding, row.original)
        if not resources:
            # No referenced resources — score by integrity only.
            score = 0.5 if integrity == "ok" else 0.2
            candidate = BestSnapshot(
                epoch_idx=epoch_idx,
                snapshot_url_human=snapshot_url(row.timestamp, row.original, for_human=True),
                timestamp=row.timestamp,
                score=score,
                integrity=integrity,
                resources_total=0,
                resources_archived=0,
            )
            if best is None or candidate.score > best.score:
                best = candidate
            continue

        # Sample resources to keep Availability API calls bounded.
        MAX_RES = max_resources_per_candidate
        res_sample = (
            resources if len(resources) <= MAX_RES
            else [resources[i] for i in range(0, len(resources),
                                              max(1, len(resources) // MAX_RES))][:MAX_RES]
        )
        await _emit(
            f"эпоха #{epoch_idx+1}: кандидат {cand_idx}/{len(chosen)} — "
            f"проверяю {len(res_sample)} ресурсов через Availability API"
        )
        by_type: dict[str, list[int]] = {}  # type -> [total, archived]
        missing: list[str] = []
        archived_count = 0
        for ru in res_sample:
            t = _classify_resource(ru)
            by_type.setdefault(t, [0, 0])
            by_type[t][0] += 1
            ok = await _check_resource(http, throttle, ru, row.timestamp)
            if ok:
                by_type[t][1] += 1
                archived_count += 1
            else:
                if len(missing) < 8:
                    missing.append(ru)

        ratio = archived_count / max(1, len(res_sample))
        # Integrity bonus/penalty.
        integrity_w = 1.0 if integrity == "ok" else 0.5
        score = round(ratio * integrity_w, 3)

        candidate = BestSnapshot(
            epoch_idx=epoch_idx,
            snapshot_url_human=snapshot_url(row.timestamp, row.original, for_human=True),
            timestamp=row.timestamp,
            score=score,
            integrity=integrity,
            resources_total=len(res_sample),
            resources_archived=archived_count,
            by_type={k: (v[0], v[1]) for k, v in by_type.items()},
            missing=missing,
        )
        if best is None or candidate.score > best.score:
            best = candidate

    return best


def filter_home_page_rows(rows: Sequence[CdxRow], source_domain: str) -> list[CdxRow]:
    """Filter 200-status CDX rows to those that look like the home page."""
    return [r for r in rows
            if r.statuscode == "200" and _is_home_page(r.original, source_domain)]


def epoch_candidates(
    rows: Sequence[CdxRow], period_from: datetime, period_to: datetime,
) -> list[CdxRow]:
    """Restrict candidates to the epoch's time window."""
    out: list[CdxRow] = []
    for r in rows:
        ts = _ts_to_dt(r.timestamp)
        if ts is None:
            continue
        if period_from <= ts <= period_to:
            out.append(r)
    out.sort(key=lambda r: r.timestamp)
    return out
