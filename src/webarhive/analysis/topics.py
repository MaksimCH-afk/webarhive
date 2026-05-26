"""Topic-epoch assembly (spec §6 full pipeline).

Input:  list of 200-status CDX rows for a domain (already collapsed by
        digest server-side).
Output: list of TopicEpoch records (period → category → confidence → reason).

Pipeline (per spec):
    1. light fetch (title + meta description + h1) for each version
       — actually we already need at least title for shift detection;
       we'll do the cheap fetch only for survivors of fingerprint dedup
       at the URL level (same digest already collapsed by CDX).
    2. shift detection: only versions where fingerprint diverges by more
       than TITLE_SHIFT_THRESHOLD words go forward.
    3. heavy fetch + LLM classification ONLY for shift points.
    4. merge consecutive same-category versions into epochs.

Budget protection:
    - MAX_LLM_CALLS_PER_DOMAIN caps how many heavy classifications a
      single domain can trigger. If hit → returns partial=True so the
      orchestrator can mark the domain as `partial` (spec §11, §19).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from webarhive.analysis.history import _parse_ts
from webarhive.analysis.topic_shift import VersionFingerprint, is_shift
from webarhive.cdx.client import CdxRow
from webarhive.config.categories import CATEGORY_BY_KEY, FALLBACK_CATEGORY, is_risky
from webarhive.fetcher.parser import ParsedPage, parse_html
from webarhive.fetcher.snapshot import SnapshotContent, SnapshotFetcher, snapshot_url
from webarhive.llm.client import OpenRouterClient
from webarhive.llm.prompts import build_classification_prompt

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VersionInfo:
    row: CdxRow
    captured_at: datetime
    light: ParsedPage | None = None
    heavy: ParsedPage | None = None
    category: str | None = None
    confidence: float | None = None
    reason: str | None = None
    snapshot_url_human: str = ""
    classified: bool = False  # True if LLM was called on this version

    @property
    def fingerprint(self) -> VersionFingerprint:
        p = self.heavy or self.light
        if p is None:
            return VersionFingerprint.from_fields("", "", "")
        return VersionFingerprint.from_fields(p.title, p.description, p.h1)


@dataclass(slots=True)
class TopicEpoch:
    period_from: datetime
    period_to: datetime
    category: str
    confidence: float | None
    reason: str | None
    sample_snapshot_url: str
    versions_in_epoch: int = 1


@dataclass(slots=True)
class TopicResult:
    epochs: list[TopicEpoch] = field(default_factory=list)
    versions: list[VersionInfo] = field(default_factory=list)
    llm_calls_used: int = 0
    partial: bool = False  # hit the MAX_LLM_CALLS_PER_DOMAIN budget

    @property
    def risky_categories(self) -> set[str]:
        return {e.category for e in self.epochs if is_risky(e.category)}


# Type alias for the per-call audit callback (records to DB).
AuditFn = Callable[..., Awaitable[None]]


async def _fetch_light(
    fetcher: SnapshotFetcher, row: CdxRow
) -> ParsedPage | None:
    try:
        content: SnapshotContent = await fetcher.fetch(row.timestamp, row.original)
    except Exception as exc:
        logger.warning("light fetch failed for %s %s: %s", row.timestamp, row.original, exc)
        return None
    if not content.body:
        return None
    return parse_html(content.body, encoding=content.encoding, text_limit=None)


async def _fetch_heavy(
    fetcher: SnapshotFetcher, row: CdxRow, *, text_limit: int
) -> ParsedPage | None:
    try:
        content: SnapshotContent = await fetcher.fetch(row.timestamp, row.original)
    except Exception as exc:
        logger.warning("heavy fetch failed for %s %s: %s", row.timestamp, row.original, exc)
        return None
    if not content.body:
        return None
    return parse_html(content.body, encoding=content.encoding, text_limit=text_limit)


def _coerce_category(parsed: dict | None) -> tuple[str, float | None, str | None]:
    """Validate LLM JSON against the enum. Spec §6: invalid → fallback."""
    if not parsed:
        return FALLBACK_CATEGORY, None, None
    cat = parsed.get("category")
    if not isinstance(cat, str) or cat not in CATEGORY_BY_KEY:
        return FALLBACK_CATEGORY, None, None
    confidence = parsed.get("confidence")
    try:
        confidence_f = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_f = None
    reason = parsed.get("reason") if isinstance(parsed.get("reason"), str) else None
    return cat, confidence_f, reason


def _merge_into_epochs(versions: Sequence[VersionInfo]) -> list[TopicEpoch]:
    """Glue consecutive same-category versions into epochs (spec §6)."""
    epochs: list[TopicEpoch] = []
    for v in versions:
        if v.category is None:
            continue
        if epochs and epochs[-1].category == v.category:
            ep = epochs[-1]
            ep.period_to = v.captured_at
            ep.versions_in_epoch += 1
            # Keep first-seen confidence/reason — earliest evidence for the epoch.
        else:
            epochs.append(
                TopicEpoch(
                    period_from=v.captured_at,
                    period_to=v.captured_at,
                    category=v.category,
                    confidence=v.confidence,
                    reason=v.reason,
                    sample_snapshot_url=v.snapshot_url_human,
                    versions_in_epoch=1,
                )
            )
    return epochs


async def classify_topics(
    rows: Sequence[CdxRow],
    *,
    fetcher: SnapshotFetcher,
    llm: OpenRouterClient,
    model: str,
    text_limit: int,
    title_shift_threshold: int,
    max_llm_calls: int,
    audit: AuditFn | None = None,
    light_fetch_cap: int = 120,
    progress=None,
) -> TopicResult:
    """Run the full 3-stage pipeline on 200-status CDX rows.

    Rows must already be 200-only and chronologically sorted ascending.
    `light_fetch_cap` — если версий больше, отбираем равномерно по
    времени; иначе light fetch на 1000+ версий на доменах с богатым
    архивом превращается в часы ожидания (IA throttle + параллельные
    воркеры). Когда оператор хочет полноту, он повысит этот лимит.

    `progress` — async callable (msg: str) — куда стримить прогресс
    в трассировку домена; вызывается на каждый шаг (каждые ~25 версий
    в light fetch + переход stage'ов).
    """
    result = TopicResult()
    if not rows:
        return result

    # Build VersionInfo carcasses with parsed timestamps.
    versions: list[VersionInfo] = []
    for row in rows:
        ts = _parse_ts(row.timestamp)
        if ts is None:
            continue
        versions.append(VersionInfo(
            row=row,
            captured_at=ts,
            snapshot_url_human=snapshot_url(row.timestamp, row.original, for_human=True),
        ))
    if not versions:
        return result
    versions.sort(key=lambda v: v.captured_at)

    # Sampling: на доменах с большим архивом (1500+ live versions) light
    # fetch на каждую версию становится узким местом — на rate=4 req/s
    # это часы только на одну стадию. Сэмплируем равномерно по времени:
    # первая, последняя и evenly-spaced остальные. Версии, выпавшие из
    # сэмпла, всё равно «доедут» до эпох — forward-fill из соседних.
    total_versions = len(versions)
    if total_versions > light_fetch_cap:
        step = (total_versions - 1) / (light_fetch_cap - 1)
        keep_indices = {int(round(i * step)) for i in range(light_fetch_cap)}
        sampled = [versions[i] for i in sorted(keep_indices)]
        if progress is not None:
            await progress(
                f"тематика: {total_versions} версий — сэмплируем "
                f"{len(sampled)} равномерно по времени (cap={light_fetch_cap})"
            )
    else:
        sampled = versions

    # Stage 1: light fetch с прогрессом. На rate~4/sec и параллельных
    # воркерах раньше эта фаза молча висела минутами. Теперь отчитываемся.
    async def _light_task(v: VersionInfo) -> None:
        v.light = await _fetch_light(fetcher, v.row)

    if progress is not None:
        await progress(f">>> light fetch для {len(sampled)} версий")
    # Идём батчами по 25, чтобы между батчами писать в трассировку
    # «25/356», «50/356», … Тогда видно, что фаза идёт, а не висит.
    BATCH = 25
    done = 0
    for i in range(0, len(sampled), BATCH):
        chunk = sampled[i : i + BATCH]
        await asyncio.gather(*(_light_task(v) for v in chunk))
        done += len(chunk)
        if progress is not None and (done < len(sampled) or done == len(sampled)):
            await progress(f"light fetch: {done}/{len(sampled)}")

    # Дальше работаем только с sampled — остальные позже получат
    # категорию через forward-fill.
    versions = sampled

    # Stage 2: shift detection — spec §6 etap 2 wording is "соседних
    # версий", so each version is compared against its IMMEDIATE
    # predecessor (not against the last shifted point). The first
    # version is always treated as a shift point so it gets classified.
    shift_indices: list[int] = [0]
    for i in range(1, len(versions)):
        prev_fp = versions[i - 1].fingerprint
        curr_fp = versions[i].fingerprint
        if is_shift(prev_fp, curr_fp, threshold=title_shift_threshold):
            shift_indices.append(i)

    # Stage 3: heavy fetch + LLM only on shift points.
    for idx in shift_indices:
        if result.llm_calls_used >= max_llm_calls:
            result.partial = True
            logger.info("topics: hit max_llm_calls=%d, marking partial", max_llm_calls)
            break
        v = versions[idx]
        v.heavy = await _fetch_heavy(fetcher, v.row, text_limit=text_limit)
        if v.heavy is None or (not v.heavy.title and not v.heavy.description
                               and not v.heavy.h1 and not v.heavy.body_text):
            # Empty content — record as пусто_нет_контента without LLM call.
            v.category = "пусто_нет_контента"
            v.confidence = None
            v.reason = "пустой снапшот"
            v.classified = False
            continue

        sys_p, usr_p = build_classification_prompt(
            title=v.heavy.title,
            description=v.heavy.description,
            h1=v.heavy.h1,
            body_text=v.heavy.body_text,
        )
        response = await llm.chat_json(
            model=model,
            system_prompt=sys_p,
            user_prompt=usr_p,
        )
        result.llm_calls_used += 1
        v.classified = True

        cat, conf, reason = _coerce_category(response.parsed)
        v.category = cat
        v.confidence = conf
        v.reason = reason

        if audit is not None:
            await audit(
                role="classification",
                model=response.model,
                snapshot_url_value=v.snapshot_url_human,
                input_text=usr_p,
                output=response.parsed,
                raw_output=response.raw_text,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
                error=response.error,
            )

    # Forward-fill category for the non-shift versions: they share the
    # category of the most recent classified version (since by definition
    # they didn't shift past the threshold).
    current: VersionInfo | None = None
    for v in versions:
        if v.category is not None:
            current = v
            continue
        if current is not None:
            v.category = current.category
            v.confidence = current.confidence
            v.reason = current.reason

    result.versions = versions
    result.epochs = _merge_into_epochs(versions)
    return result
