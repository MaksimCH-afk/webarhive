"""Redirect classification (spec §7).

For each 3xx CDX row we fetch the snapshot, read the Location header
(or extract from body as a last resort), then classify:

  - technical    — www↔root, http↔https, same-domain or to subdomain
                   (uninteresting, not surfaced to the operator)
  - same_site    — same registrable root name across zones
  - company_move — different name, but brand match in title/content
  - review       — neither, OR borderline case (safe default per §7.1)

A `snapshot_url` (without `id_`) is attached to every review-tagged
record (spec §7.2) — without it the tag is not emitted.

Comparison uses the registrable root via PSL (tldextract). Without PSL
`domain.co.uk` would yield root=`co`/`uk` and break classification.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import tldextract

from webarhive.analysis.history import _parse_ts
from webarhive.db.models import RedirectClass
from webarhive.fetcher.snapshot import SnapshotFetcher, snapshot_url
from webarhive.llm.client import OpenRouterClient
from webarhive.llm.prompts import build_redirect_prompt

logger = logging.getLogger(__name__)

_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv=['"]?refresh['"]?[^>]+content=['"]?\s*\d+\s*;\s*url=([^'">\s]+)""",
    re.IGNORECASE,
)

# Wayback wraps the original URL after `id_/`. Pulling target out of the
# archived URL is reliable for archive-rewritten 3xx Locations.
_WAYBACK_RE = re.compile(r"/web/\d+(?:[a-z_]*)?/(https?://.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RedirectInfo:
    captured_at: datetime
    from_url: str
    to_url: str | None
    target_domain: str | None  # registrable root of target
    classification: RedirectClass
    reason: str
    snapshot_url: str | None  # human-friendly, without id_


def _registrable_root(host: str) -> tuple[str, str] | None:
    """Return (core_name, suffix). e.g. blog.foo.co.uk → ('foo', 'co.uk')."""
    if not host:
        return None
    ext = _TLD(host.lower())
    if not ext.domain or not ext.suffix:
        return None
    return ext.domain, ext.suffix


def _extract_target_from_wayback_url(wb_url: str) -> str | None:
    """If the archive rewrote the redirect to itself, peel out the
    original URL it points to."""
    m = _WAYBACK_RE.search(wb_url)
    if m:
        return m.group(1)
    return None


def _resolve_target(
    *,
    from_original: str,
    final_url: str,
    location_header: str | None,
    body: bytes,
    encoding: str | None,
) -> str | None:
    """Best-effort: extract the actual target the redirect was pointing at.
    Sources, in order:
        1. Location header (resolved against original URL if relative)
        2. final_url after Wayback's own redirect-unwrap
        3. <meta http-equiv=refresh> in body
    """
    if location_header:
        target = location_header.strip()
        if target:
            # Wayback often rewrites Location to /web/<ts>/<original>
            extracted = _extract_target_from_wayback_url(target)
            if extracted:
                return extracted
            # Resolve relative URLs against the original captured URL.
            if not target.startswith(("http://", "https://", "//")):
                target = urljoin(from_original, target)
            return target

    # Sometimes Wayback already followed the redirect and final_url is
    # the resulting archive URL — try to peel the original.
    extracted = _extract_target_from_wayback_url(final_url)
    if extracted:
        return extracted

    # Meta refresh in body
    if body:
        try:
            text = body.decode(encoding or "utf-8", errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        m = _META_REFRESH_RE.search(text[:4096])
        if m:
            return m.group(1)

    return None


def _classify_pair(
    *, source_domain: str, target_url: str | None
) -> tuple[RedirectClass, str, str | None]:
    """Pure classifier — no LLM, no fetch. Returns (class, reason, target_root)."""
    if not target_url:
        return RedirectClass.REVIEW, "не удалось извлечь цель редиректа", None

    parts = urlsplit(target_url)
    if not parts.hostname:
        return RedirectClass.REVIEW, "цель без хоста", None
    target_host = parts.hostname.lower().lstrip(".")
    # Strip www. only — same rule as input normalization.
    if target_host.startswith("www."):
        target_host = target_host[4:]

    source_root = _registrable_root(source_domain)
    target_root_parts = _registrable_root(target_host)
    if target_root_parts is None:
        return RedirectClass.REVIEW, "цель: не распознан суффикс", None
    target_root = f"{target_root_parts[0]}.{target_root_parts[1]}"

    # Same hostname or same registrable name — technical or same site.
    if source_root is not None:
        same_core = source_root[0] == target_root_parts[0]
        same_suffix = source_root[1] == target_root_parts[1]
        if same_core and same_suffix:
            # foo.com → foo.com OR sub.foo.com → foo.com (after normalization)
            return RedirectClass.TECHNICAL, "внутри того же домена/поддомена", target_root
        if same_core and not same_suffix:
            # foo.com → foo.net: same core name, different zone
            return RedirectClass.SAME_SITE, "то же имя, другая зона", target_root

    # Different core — needs brand evidence to be a company_move. Default
    # (per §7.1 safety): review. The LLM-redirect role (§9.3) can promote
    # to company_move when ENABLE_REDIRECT_LLM is on; the brand-from-title
    # heuristic can also promote it at the call site.
    return RedirectClass.REVIEW, "другое имя без подтверждённой связи", target_root


def _maybe_promote_company_move_by_brand(
    *,
    source_brand_tokens: set[str],
    target_brand_tokens: set[str],
) -> bool:
    """Cheap brand match (spec §9 footer): word overlap in titles."""
    if not source_brand_tokens or not target_brand_tokens:
        return False
    overlap = source_brand_tokens & target_brand_tokens
    return len(overlap) > 0


async def analyze_redirects(
    rows,
    *,
    source_domain: str,
    fetcher: SnapshotFetcher,
    cap: int = 150,
    progress=None,
) -> list[RedirectInfo]:
    """Classify a list of 3xx CDX rows for a domain.

    Returns RedirectInfo records, one per source 3xx row. Review-tagged
    records always carry a `snapshot_url` (without id_) per spec §7.2.

    `cap` — на доменах с 2000+ редиректами (бывают часто, особенно
    после кампаний с UTM-метками) фетчить каждый 3xx-снапшот через
    общий IA-троттл (~1-4 req/sec) превращается в часы. Сэмплируем
    равномерно по времени, чтобы охватить всю историю.
    `progress` — async callable(msg) для трассировки.
    """
    rows_list = list(rows)
    total = len(rows_list)
    if total > cap:
        # Равномерный сэмпл по времени, как и в light fetch.
        step = (total - 1) / (cap - 1) if cap > 1 else 0
        keep = {int(round(i * step)) for i in range(cap)}
        rows_list = [r for i, r in enumerate(rows_list) if i in keep]
        if progress is not None:
            await progress(
                f"редиректы: {total} 3xx-снапшотов — сэмплируем "
                f"{len(rows_list)} равномерно по времени (cap={cap})"
            )

    results: list[RedirectInfo] = []
    # Прогресс каждые 10 — на cap=150 это 15 чекпойнтов вместо 6.
    # Фаза часто занимает 3-5 минут на большом домене, оператору нужна
    # более частая обратная связь чтобы видеть что не зависло.
    BATCH = 10
    done = 0
    for row in rows_list:
        captured_at = _parse_ts(row.timestamp)
        if captured_at is None:
            done += 1
            continue
        try:
            content = await fetcher.fetch(row.timestamp, row.original)
        except Exception as exc:
            logger.warning("redirect fetch failed for %s: %s", row.original, exc)
            results.append(RedirectInfo(
                captured_at=captured_at,
                from_url=row.original,
                to_url=None,
                target_domain=None,
                classification=RedirectClass.REVIEW,
                reason=f"не удалось получить снапшот: {type(exc).__name__}",
                snapshot_url=snapshot_url(row.timestamp, row.original, for_human=True),
            ))
            done += 1
            if progress is not None and done % BATCH == 0:
                await progress(f"редиректы: {done}/{len(rows_list)}")
            continue

        target = _resolve_target(
            from_original=row.original,
            final_url=content.final_url,
            location_header=content.headers.get("location"),
            body=content.body,
            encoding=content.encoding,
        )
        cls, reason, target_root = _classify_pair(source_domain=source_domain, target_url=target)

        # Snapshot for human always present on review-class (and useful
        # everywhere — cheap to attach).
        human_snap = snapshot_url(row.timestamp, row.original, for_human=True)

        results.append(RedirectInfo(
            captured_at=captured_at,
            from_url=row.original,
            to_url=target,
            target_domain=target_root,
            classification=cls,
            reason=reason,
            snapshot_url=human_snap,
        ))
        done += 1
        if progress is not None and done % BATCH == 0:
            await progress(f"редиректы: {done}/{len(rows_list)}")
    if progress is not None and done % BATCH != 0:
        await progress(f"редиректы: {done}/{len(rows_list)}")
    return results


async def _fetch_target_topic_signal(
    *,
    target_domain: str,
    near_timestamp: datetime,
    cdx,
    fetcher: SnapshotFetcher,
) -> dict:
    """Spec §9.3: «мы и так фетчим тематику целевого домена».

    Light topic signal for the redirect target: find the 200-status
    capture of the target domain closest to `near_timestamp`, fetch
    its title/description/h1. No heavy parse, no LLM classification —
    just enough text for the redirect-LLM to compare brand/topic.
    """
    from webarhive.fetcher.parser import parse_html

    signal: dict = {"domain": target_domain, "title": "", "description": "", "h1": ""}
    try:
        # Server-side filter — целевой домен может быть огромным (10k+
        # захватов). Без фильтра тянем весь индекс ради двух строк ради
        # одного fetch'а ближайшего 200. С фильтром payload в 2-3 раза
        # меньше + не качаем 3xx/404/etc что нам не нужны.
        rows = await cdx.fetch_all(
            target_domain, match_type="domain", filters=("statuscode:200",),
        )
    except Exception as exc:
        logger.warning("redirect target CDX failed for %s: %s", target_domain, exc)
        signal["error"] = f"CDX недоступен: {type(exc).__name__}"
        return signal

    # filters=statuscode:200 уже даёт нам только live-снимки.
    live = rows
    if not live:
        signal["status"] = "цель мёртва в архиве"
        return signal

    target_ts_str = near_timestamp.strftime("%Y%m%d%H%M%S")
    chosen = min(live, key=lambda r: abs(int(r.timestamp[:14] or "0") - int(target_ts_str)))
    try:
        content = await fetcher.fetch(chosen.timestamp, chosen.original)
    except Exception as exc:
        signal["error"] = f"снапшот недоступен: {type(exc).__name__}"
        return signal
    if not content.body:
        signal["status"] = "пустой снапшот цели"
        return signal

    parsed = parse_html(content.body, encoding=content.encoding, text_limit=None)
    signal["title"] = parsed.title
    signal["description"] = parsed.description
    signal["h1"] = parsed.h1
    signal["captured_at"] = chosen.timestamp
    return signal


async def llm_refine_redirects(
    redirects: Sequence[RedirectInfo],
    *,
    source_topic: dict,
    llm: OpenRouterClient,
    model: str,
    fetcher: SnapshotFetcher,
    cdx=None,
    review_cap: int = 30,
    llm_parallelism: int = 16,
    progress=None,
) -> list[RedirectInfo]:
    """Spec §9.3 — only on REVIEW-class borderline cases. Asks the model
    'same site / company move / hijack' based on topic of both sides;
    safety default §7.1 preserved: uncertain → stays REVIEW.

    `review_cap` — потолок числа REVIEW-редиректов, которые мы
    реально обогащаем (CDX + LLM). Сверх потолка остальные REVIEW
    оставляем как есть. На доменах с тысячами 3xx этот пайплайн без
    cap'а уходит в часы.

    `llm_parallelism` — скользящее окно параллельных вызовов. Раньше
    эта фаза шла последовательно и на 20-30 REVIEW занимала 15-25
    минут с CDX-обогащением цели (полный скан + light fetch + LLM
    на каждый, всё await). Теперь — ~30-60 секунд.

    If a `cdx` client is supplied, we fetch the target domain's nearest
    200-snapshot title/desc/h1 so the model actually sees topic on both
    sides. Without cdx we degrade to {domain, url}. Если REVIEW-
    редиректов больше `review_cap`, мы автоматически отключаем CDX-
    обогащение (слишком дорого) и идём в лёгком режиме.
    """
    review_count = sum(1 for r in redirects
                       if r.classification is RedirectClass.REVIEW and r.to_url)
    use_cdx = cdx
    if review_count > review_cap and cdx is not None:
        use_cdx = None
        if progress is not None:
            await progress(
                f"LLM-редиректы: REVIEW={review_count} > cap={review_cap} — "
                f"отключаю CDX-обогащение цели (слишком дорого), "
                f"остаюсь на тематике (domain+url)"
            )

    # Собираем индексы REVIEW-редиректов, реально подлежащих обработке.
    review_indices: list[int] = []
    skipped_over_cap = 0
    for i, r in enumerate(redirects):
        if r.classification is not RedirectClass.REVIEW or not r.to_url:
            continue
        if len(review_indices) < review_cap:
            review_indices.append(i)
        else:
            skipped_over_cap += 1

    if progress is not None:
        await progress(
            f"LLM-refine редиректов: начинаю обработку "
            f"{len(review_indices)} REVIEW (параллелизм={llm_parallelism}"
            + (", CDX-обогащение цели вкл" if use_cdx is not None else "")
            + ")"
        )

    refined: dict[int, RedirectInfo] = {}
    sem = asyncio.Semaphore(max(1, int(llm_parallelism)))
    completed = 0
    completed_lock = asyncio.Lock()

    async def _refine_one(idx: int) -> None:
        nonlocal completed
        async with sem:
            r = redirects[idx]
            target_topic: dict
            if use_cdx is not None and r.target_domain:
                target_topic = await _fetch_target_topic_signal(
                    target_domain=r.target_domain,
                    near_timestamp=r.captured_at,
                    cdx=use_cdx,
                    fetcher=fetcher,
                )
            else:
                target_topic = {"domain": r.target_domain, "url": r.to_url}
            sys_p, usr_p = build_redirect_prompt(from_topic=source_topic, to_topic=target_topic)
            resp = await llm.chat_json(model=model, system_prompt=sys_p, user_prompt=usr_p)
            parsed = resp.parsed or {}
            relation = str(parsed.get("relation", "")).lower()
            mapping = {
                "тот_же_сайт": RedirectClass.SAME_SITE,
                "переезд_компании": RedirectClass.COMPANY_MOVE,
                # `перехват` → keep REVIEW
            }
            new_cls = mapping.get(relation, RedirectClass.REVIEW)
            new_reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else r.reason
            refined[idx] = RedirectInfo(
                captured_at=r.captured_at,
                from_url=r.from_url,
                to_url=r.to_url,
                target_domain=r.target_domain,
                classification=new_cls,
                reason=f"LLM: {new_reason}" if new_reason else r.reason,
                snapshot_url=r.snapshot_url,
            )
            async with completed_lock:
                completed += 1
                # Прогресс каждые 5 — на cap=30 это 6 чекпойнтов
                if progress is not None and completed % 5 == 0:
                    await progress(
                        f"LLM-редиректы: {completed}/{len(review_indices)}"
                    )

    await asyncio.gather(*(_refine_one(i) for i in review_indices))

    # Сборка выходного списка с сохранением исходного порядка.
    out: list[RedirectInfo] = []
    for i, r in enumerate(redirects):
        out.append(refined.get(i, r))

    if progress is not None and skipped_over_cap:
        await progress(
            f"LLM-редиректы: {skipped_over_cap} оставлены REVIEW без LLM "
            f"(превысили cap={review_cap})"
        )
    return out
