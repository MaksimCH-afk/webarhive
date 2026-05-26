"""Per-domain pipeline + per-run orchestrator (spec §2.1–2.2, §18).

Single-domain pipeline (process_domain):
    CDX → bucketize → history summary
        → topics (light fetch → shift → heavy + LLM → epochs)
        → redirects (fetch 3xx → resolve target → classify)
        → drops (gaps + topic change → heuristic; opt LLM)
        → verdict (opt LLM; flags always)
    Result is written to DB as soon as it's ready (spec §2.2 — falling
    over on the last domain doesn't lose the rest).

Run orchestrator (run_pipeline):
    Pulls pending domains, spawns up to CONCURRENCY worker tasks. The
    shared IA throttle gates all IA traffic across workers (spec §2.1
    "bottleneck is throttling, not CPU").

Resumability (spec §2.2):
    On restart of an interrupted run, only `pending|running` domains are
    re-picked. Finished ones (done|partial|error|no_data) are skipped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from webarhive.analysis.drops import find_gaps, score_drops, smart_drop_assess
from webarhive.analysis.history import summarize_history
from webarhive.analysis.redirects import RedirectInfo, analyze_redirects, llm_refine_redirects
from webarhive.analysis.topics import TopicResult, classify_topics
from webarhive.analysis.verdict import make_verdict
from webarhive.cdx.client import CdxClient
from webarhive.cdx.throttle import IAThrottle
from webarhive.db import (
    Domain,
    DomainStatus,
    Drop,
    Epoch,
    Redirect,
    Run,
    RunStatus,
    Verdict,
    create_engine_and_session,
)
from webarhive.db.repo import (
    create_run,
    finish_run,
    get_pending_for_run,
    increment_run_counters,
    record_llm_call,
    seed_domains,
)
from webarhive.fetcher.snapshot import SnapshotFetcher
from webarhive.llm.client import OpenRouterClient
from webarhive.logging_.tracer import DomainTracer

logger = logging.getLogger(__name__)


async def process_domain(
    *,
    domain_row: Domain,
    run_id: int,
    snapshot: dict,
    session_factory: async_sessionmaker,
    cdx: CdxClient,
    fetcher: SnapshotFetcher,
    llm: OpenRouterClient | None,
    whois_client=None,
) -> None:
    """End-to-end pipeline for a single domain. Writes results to DB
    incrementally and updates the run counters at the end."""
    # Live-flush trace to DB on every write — so if the worker hangs
    # we still see exactly where it got stuck (instead of an empty box
    # until the function returns).
    async def _flush_trace(text: str) -> None:
        try:
            async with session_factory() as s:
                d_row = await s.get(Domain, domain_row.id)
                if d_row is not None:
                    d_row.trace = text
                    await s.commit()
        except Exception:
            logger.exception("trace flush failed for %s", domain_row.domain)

    tracer = DomainTracer(domain_row.domain, flush_fn=_flush_trace)
    started_at = datetime.utcnow()

    models = snapshot["models"]
    roles = snapshot["roles"]
    limits = snapshot["limits"]
    inp = snapshot["input"]

    match_type = "host" if inp.get("check_subdomains") else "domain"

    # Mark domain running.
    async with session_factory() as s:
        d: Domain | None = await s.get(Domain, domain_row.id)
        if d is None:
            return
        d.status = DomainStatus.RUNNING.value
        d.started_at = started_at
        await s.commit()

    final_status = DomainStatus.DONE
    error_message: str | None = None
    verdict_for_counter: Verdict | None = None
    topic_partial = False

    try:
        # --- CDX ---
        tracer.step("CDX", f"matchType={match_type}")
        cdx_rows = await cdx.fetch_all(domain_row.domain, match_type=match_type)
        buckets_total = {
            "200": sum(1 for r in cdx_rows if r.status_bucket == "200"),
            "3xx": sum(1 for r in cdx_rows if r.status_bucket == "3xx"),
            "404": sum(1 for r in cdx_rows if r.status_bucket == "404"),
            "5xx": sum(1 for r in cdx_rows if r.status_bucket == "5xx"),
            "other": sum(1 for r in cdx_rows if r.status_bucket == "other"),
        }
        history = summarize_history(cdx_rows)

        # Client-side digest dedup (spec §6 «по версиям после схлопывания
        # по digest, а НЕ по каждому снапшоту»). CDX-server collapse уже
        # схлопнул дубли внутри одной URL-группы; здесь мы дополнительно
        # схлопываем по digest через ВСЕ URL — если index.html, /home и
        # /?utm=x сохранены с одинаковым контентом, это одна версия.
        # Только для 200-rows — редиректы/404 оставляем как есть, у них
        # digest не несёт смысла.
        live_before_dedup = len(history.live_versions)
        seen_digest: set[str] = set()
        deduped_live: list = []
        for r in history.live_versions:
            if r.digest and r.digest in seen_digest:
                continue
            if r.digest:
                seen_digest.add(r.digest)
            deduped_live.append(r)
        history.by_bucket["200"] = deduped_live
        dropped = live_before_dedup - len(deduped_live)

        # Реальная сводка с двумя цифрами: до клиентского dedup и после.
        tracer.cdx_summary(
            total=len(cdx_rows),
            after_collapse=len(cdx_rows) - dropped,
            buckets=buckets_total,
        )
        if dropped:
            tracer.info(
                f"digest-дедуп: 200-снапшотов {live_before_dedup} → "
                f"{len(deduped_live)} уникальных версий "
                f"(отброшено {dropped} дублей)"
            )

        if not cdx_rows:
            final_status = DomainStatus.NO_DATA
            tracer.warn("домена нет в архиве — нет данных для анализа")

        # --- Topics (only on 200 rows, chronologically sorted) ---
        topic_result = TopicResult()
        if deduped_live and llm is not None:
            sorted_live = sorted(deduped_live, key=lambda r: r.timestamp)
            tracer.step("ТЕМАТИКА", f"{len(sorted_live)} версий со статусом 200")

            audit_run_id = run_id
            audit_domain_id = domain_row.id
            text_limit = limits["text_limit"]

            async def audit(*, role, model, snapshot_url_value, input_text, output,
                            raw_output, prompt_tokens, completion_tokens,
                            cost_usd, latency_ms, error=None):
                async with session_factory() as s:
                    await record_llm_call(
                        s,
                        run_id=audit_run_id, domain_id=audit_domain_id,
                        role=role, model=model,
                        snapshot_url=snapshot_url_value,
                        input_text=input_text, text_limit=text_limit,
                        output=output, raw_output=raw_output,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cost_usd=cost_usd, latency_ms=latency_ms, error=error,
                    )
                    await s.commit()
                tracer.llm_call(role=role, model=model,
                                tokens_in=prompt_tokens,
                                latency_ms=latency_ms)

            async def _topic_progress(msg: str) -> None:
                tracer.info(msg)

            topic_result = await classify_topics(
                sorted_live,
                fetcher=fetcher,
                llm=llm,
                model=models["classification"],
                text_limit=text_limit,
                title_shift_threshold=limits["title_shift_threshold"],
                max_llm_calls=limits["max_llm_calls_per_domain"],
                audit=audit,
                light_fetch_cap=int(limits.get("light_fetch_cap", 120)),
                progress=_topic_progress,
            )
            topic_partial = topic_result.partial
            tracer.topics_plan(
                total_versions=len(sorted_live),
                shift_points=len([v for v in topic_result.versions if v.classified]),
                llm_budget=limits["max_llm_calls_per_domain"],
            )
        elif deduped_live and llm is None:
            tracer.warn("LLM-клиент не подключён — тематика не классифицирована")

        # --- Redirects ---
        redirects: list[RedirectInfo] = []
        if history.redirect_rows:
            tracer.step("РЕДИРЕКТЫ", f"{len(history.redirect_rows)} 3xx-снапшотов")
            t0 = datetime.utcnow()
            redirects = await analyze_redirects(
                history.redirect_rows,
                source_domain=domain_row.domain,
                fetcher=fetcher,
            )
            tracer.info(
                f"редиректы: классифицировано {len(redirects)} за "
                f"{(datetime.utcnow() - t0).total_seconds():.1f}s"
            )
            if roles.get("redirect_llm") and llm is not None and redirects:
                # spec §9.3: only borderline (REVIEW) cases get the LLM tiebreaker
                source_topic = {
                    "domain": domain_row.domain,
                    "categories": [e.category for e in topic_result.epochs],
                }
                redirects = await llm_refine_redirects(
                    redirects,
                    source_topic=source_topic,
                    llm=llm,
                    model=models["redirect"],
                    fetcher=fetcher,
                    cdx=cdx,
                )
                tracer.info("LLM-уточнение редиректов выполнено")

        # --- Drops: feed gap-detection both 200-versions and 404 markers ---
        gap_times: list[datetime] = [v.captured_at for v in topic_result.versions]
        for row in history.not_found_rows:
            ts = _safe_parse(row.timestamp)
            if ts:
                gap_times.append(ts)
        gaps = find_gaps(gap_times)
        drop_signals = score_drops(gaps, topic_result.epochs)
        if gaps:
            tracer.info(f"дропы: разрывов {len(gaps)}, оценено {sum(1 for d in drop_signals if d.is_drop)}")
        if roles.get("smart_drop") and llm is not None and drop_signals:
            drop_signals = await smart_drop_assess(
                drop_signals, llm=llm, model=models["smart_drop"],
            )
            tracer.info("LLM-оценка дропов выполнена")

        # --- Verdict ---
        verdict_result = await make_verdict(
            enabled=bool(roles.get("verdict")) and llm is not None,
            domain=domain_row.domain,
            age_days=history.age_days,
            epochs=topic_result.epochs,
            redirects=redirects,
            drops=drop_signals,
            partial=topic_partial,
            llm=llm,
            model=models["verdict"],
        )
        if verdict_result.llm_response is not None:
            # Audit verdict call too.
            r = verdict_result.llm_response
            async with session_factory() as s:
                await record_llm_call(
                    s,
                    run_id=run_id, domain_id=domain_row.id,
                    role="verdict", model=r.model, snapshot_url=None,
                    input_text=None, text_limit=limits["text_limit"],
                    output=r.parsed, raw_output=r.raw_text,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    cost_usd=r.cost_usd, latency_ms=r.latency_ms,
                    error=r.error,
                )
                await s.commit()
        verdict_for_counter = verdict_result.verdict

        # --- WHOIS: реальная дата регистрации (если включено) ---
        whois_reg_date = None
        whois_status_str = None
        whois_cfg = snapshot.get("whois", {}) or {}
        if whois_cfg.get("enabled") and whois_client is not None:
            from webarhive.db.repo import whois_cache_get, whois_cache_put
            ttl = int(whois_cfg.get("cache_ttl_days", 90))
            cached = None
            async with session_factory() as s:
                cached = await whois_cache_get(s, domain_row.domain, ttl)
            if cached is not None:
                whois_reg_date, prev_status = cached
                whois_status_str = "from_cache"
                tracer.info(
                    f"WHOIS: из кэша · регистрация {whois_reg_date.date() if whois_reg_date else '—'}"
                )
            else:
                tracer.step("WHOIS")
                r = await whois_client.lookup(domain_row.domain)
                whois_reg_date = r.registration_date
                whois_status_str = r.status
                async with session_factory() as s:
                    await whois_cache_put(
                        s, domain_row.domain,
                        registration_date=whois_reg_date,
                        raw_status=r.status,
                    )
                    await s.commit()
                rem_part = f" · осталось {r.remaining_requests}" if r.remaining_requests is not None else ""
                if r.status == "got":
                    tracer.info(f"WHOIS: регистрация {r.registration_date.date()}{rem_part}")
                elif r.status == "limit":
                    tracer.warn(f"WHOIS: лимит исчерпан{rem_part}")
                else:
                    tracer.warn(f"WHOIS: {r.status} — {r.error or ''}{rem_part}")

        # --- Best snapshot (если включено) ---
        epoch_best: dict[int, "object"] = {}
        bs_cfg = snapshot.get("best_snapshot", {}) or {}
        if bs_cfg.get("enabled") and topic_result.epochs:
            from webarhive.analysis.best_snapshot import (
                best_snapshot_for_epoch,
                epoch_candidates,
                filter_home_page_rows,
            )
            tracer.step("ЛУЧШИЙ_СЛЕПОК", f"эпох: {len(topic_result.epochs)}")
            top_n = int(bs_cfg.get("top_n", 5))
            home_rows = filter_home_page_rows(cdx_rows, domain_row.domain)
            for i, ep in enumerate(topic_result.epochs):
                cands = epoch_candidates(home_rows, ep.period_from, ep.period_to)
                if not cands:
                    tracer.info(f"эпоха #{i+1}: лучший слепок недоступен (нет 200-главной)")
                    continue
                try:
                    best = await best_snapshot_for_epoch(
                        epoch_idx=i, candidates=cands,
                        source_domain=domain_row.domain,
                        fetcher=fetcher, cdx=cdx, top_n=top_n,
                    )
                except Exception as exc:
                    tracer.warn(f"эпоха #{i+1}: best-snap упал — {exc}")
                    continue
                if best is not None:
                    epoch_best[i] = best
                    tracer.info(
                        f"эпоха #{i+1}: best {best.timestamp[:8]} · "
                        f"score {best.score} · ресурсов {best.resources_archived}/"
                        f"{best.resources_total}"
                    )

        # --- Persist result components ---
        async with session_factory() as s:
            d = await s.get(Domain, domain_row.id)
            assert d is not None
            d.first_capture_at = history.first_capture_at
            d.last_capture_at = history.last_capture_at
            d.age_days = history.age_days
            d.total_captures = history.total_captures
            # «версии после схлопывания» — уникальные digest 200-снапшотов
            # (то, что реально пошло в анализ тематики) + редиректы как
            # сигнал поведения, без дедупа (они в анализе тематики не
            # участвуют, но в карточке — отдельный счётчик).
            d.total_versions = len(deduped_live) + len(history.redirect_rows)
            d.risky_flag_count = verdict_result.risky_flag_count
            d.review_flag_count = verdict_result.review_flag_count
            d.verdict = verdict_result.verdict.value if verdict_result.verdict else None
            d.verdict_reason = verdict_result.reason
            d.verdict_key_flags = verdict_result.key_flags

            # WHOIS attachments
            if whois_status_str is not None:
                d.whois_registration_date = whois_reg_date
                d.whois_status = whois_status_str
                d.whois_fetched_at = datetime.utcnow()

            # Epochs (с опциональным best_snapshot)
            for i, ep in enumerate(topic_result.epochs):
                bs = epoch_best.get(i)
                s.add(Epoch(
                    domain_id=d.id,
                    period_from=ep.period_from,
                    period_to=ep.period_to,
                    category=ep.category,
                    confidence=ep.confidence,
                    reason=ep.reason,
                    sample_snapshot_url=ep.sample_snapshot_url,
                    versions_in_epoch=ep.versions_in_epoch,
                    best_snapshot_url=(bs.snapshot_url_human if bs else None),
                    best_snapshot_ts=(bs.timestamp if bs else None),
                    best_snapshot_score=(bs.score if bs else None),
                    best_snapshot_detail=({
                        "resources_total": bs.resources_total,
                        "resources_archived": bs.resources_archived,
                        "by_type": bs.by_type,
                        "integrity": bs.integrity,
                        "missing": bs.missing,
                    } if bs else None),
                ))

            # Redirects — spec §7 says technical redirects are "не
            # интересны, отмечаем как технические" — so we save them
            # marked as such, and the UI hides them behind a collapsed
            # section. Don't drop them from the DB.
            for r in redirects:
                s.add(Redirect(
                    domain_id=d.id,
                    captured_at=r.captured_at,
                    from_url=r.from_url,
                    to_url=r.to_url,
                    target_domain=r.target_domain,
                    classification=r.classification.value,
                    reason=r.reason,
                    snapshot_url=r.snapshot_url,
                ))

            # Drops
            for ds in drop_signals:
                s.add(Drop(
                    domain_id=d.id,
                    gap_from=ds.gap_from,
                    gap_to=ds.gap_to,
                    gap_days=ds.gap_days,
                    category_before=ds.category_before,
                    category_after=ds.category_after,
                    is_drop=ds.is_drop,
                    confidence=ds.confidence,
                    reason=ds.reason,
                    source=ds.source,
                ))

            if topic_partial:
                d.status = DomainStatus.PARTIAL.value
                final_status = DomainStatus.PARTIAL
            else:
                d.status = final_status.value
            d.finished_at = datetime.utcnow()

            await s.commit()

        tracer.finish(
            age_days=history.age_days,
            epochs=len(topic_result.epochs),
            flags=verdict_result.risky_flag_count + verdict_result.review_flag_count,
            verdict=verdict_result.verdict.value if verdict_result.verdict else None,
            partial=topic_partial,
        )

    except Exception as exc:
        logger.exception("domain pipeline failed for %s", domain_row.domain)
        tracer.error(f"ошибка пайплайна: {type(exc).__name__}: {exc}")
        final_status = DomainStatus.ERROR
        error_message = f"{type(exc).__name__}: {exc}"
    finally:
        # Always flush trace + counters, even on error. Drain pending
        # live-flushes first so we don't race with a stale background
        # task that would overwrite the final state.
        await tracer.drain()
        async with session_factory() as s:
            d = await s.get(Domain, domain_row.id)
            if d is not None:
                d.trace = tracer.text()
                if final_status is DomainStatus.ERROR:
                    d.status = DomainStatus.ERROR.value
                    d.error_message = error_message
                    d.finished_at = datetime.utcnow()
                await increment_run_counters(
                    s, run_id,
                    verdict=verdict_for_counter,
                    error=(final_status is DomainStatus.ERROR),
                )
            await s.commit()


def _safe_parse(ts: str):
    from webarhive.analysis.history import _parse_ts
    return _parse_ts(ts)


async def start_run(
    *,
    domains: Iterable[str],
    settings_snapshot: dict,
    note: str | None = None,
    session_factory: async_sessionmaker | None = None,
) -> int:
    """Create a run record + seed domain rows. Returns run_id."""
    if session_factory is None:
        _, session_factory = create_engine_and_session()
    domains_list = list(domains)
    async with session_factory() as s:
        run: Run = await create_run(
            s, total=len(domains_list),
            settings_snapshot=settings_snapshot,
            note=note,
        )
        await seed_domains(s, run.id, domains_list)
        await s.commit()
        return run.id


async def run_pipeline(
    *,
    run_id: int,
    settings_snapshot: dict,
    api_key: str,
    whois_api_key: str = "",
    session_factory: async_sessionmaker | None = None,
) -> None:
    """Process all pending domains of a run with bounded concurrency.

    Idempotent: re-running on the same run_id resumes from pending/running
    rows (those still in progress when the worker died), skipping finished ones.
    """
    if session_factory is None:
        _, session_factory = create_engine_and_session()

    throttle = IAThrottle(rate=settings_snapshot["throttle"]["ia_rate_limit"])
    concurrency = settings_snapshot["throttle"]["concurrency"]
    max_retries = settings_snapshot["throttle"]["ia_max_retries"]
    backoff = settings_snapshot["throttle"]["ia_backoff"]

    # Pick up work
    async with session_factory() as s:
        pending = await get_pending_for_run(s, run_id)

    if not pending:
        async with session_factory() as s:
            await finish_run(s, run_id, status=RunStatus.FINISHED)
            await s.commit()
        return

    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        timeout=60.0,
        headers={"User-Agent": "webarhive-checker/0.1 (+internal)"},
        follow_redirects=False,
    ) as http:
        cdx = CdxClient(throttle=throttle, client=http,
                        max_retries=max_retries, backoff_base=backoff)
        fetcher = SnapshotFetcher(throttle=throttle, client=http,
                                  max_retries=max_retries, backoff_base=backoff)
        llm = OpenRouterClient(api_key=api_key) if api_key else None

        # WHOIS client only built when feature is on AND key is set.
        whois_cfg = settings_snapshot.get("whois", {}) or {}
        whois_client = None
        if whois_cfg.get("enabled") and whois_api_key:
            from webarhive.clients.whois import WhoisClient
            whois_client = WhoisClient(
                api_key=whois_api_key,
                rate_limit=float(whois_cfg.get("rate_limit", 20.0 / 60.0)),
                monthly_floor=int(whois_cfg.get("monthly_floor", 10)),
            )

        # Hard ceiling per domain so one slow domain (huge archive
        # footprint, many redirects with all LLM roles on, etc.) can't
        # block the worker pool forever. Conservatively generous —
        # marks the domain as ERROR / PARTIAL on overrun.
        per_domain_timeout = float(settings_snapshot.get("throttle", {}).get(
            "per_domain_timeout_sec", 600))

        try:
            async def worker(d: Domain) -> None:
                async with semaphore:
                    try:
                        await asyncio.wait_for(
                            process_domain(
                                domain_row=d,
                                run_id=run_id,
                                snapshot=settings_snapshot,
                                session_factory=session_factory,
                                cdx=cdx,
                                fetcher=fetcher,
                                llm=llm,
                                whois_client=whois_client,
                            ),
                            timeout=per_domain_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "domain %s exceeded %ss timeout — marking error",
                            d.domain, per_domain_timeout,
                        )
                        async with session_factory() as s:
                            row = await s.get(Domain, d.id)
                            if row is not None and row.status not in (
                                DomainStatus.DONE.value,
                                DomainStatus.PARTIAL.value,
                            ):
                                row.status = DomainStatus.ERROR.value
                                row.error_message = (
                                    f"timeout after {per_domain_timeout}s "
                                    f"(возможно, слишком много REVIEW-редиректов "
                                    f"при включённом ENABLE_REDIRECT_LLM)"
                                )
                                row.finished_at = datetime.utcnow()
                                await s.commit()
                    except Exception:
                        # process_domain already handled its own errors
                        # via try/except + finally; nothing should escape,
                        # but if it does, don't crash the whole pool.
                        logger.exception("worker for %s crashed unexpectedly", d.domain)

            await asyncio.gather(*(worker(d) for d in pending), return_exceptions=False)
        finally:
            if llm is not None:
                await llm.aclose()
            if whois_client is not None:
                await whois_client.aclose()

    async with session_factory() as s:
        await finish_run(s, run_id, status=RunStatus.FINISHED)
        await s.commit()
