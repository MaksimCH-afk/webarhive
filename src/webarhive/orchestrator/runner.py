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
    http: httpx.AsyncClient | None = None,
    throttle: IAThrottle | None = None,
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
    # Промежуточные результаты фаз — инициализируем заранее, чтобы в
    # except-блоке можно было записать в DB то, что успели посчитать
    # до сбоя. Без этого ошибка где-то в середине (LLM-refine,
    # smart_drop, …) оставляла домен с пустыми полями: 0 захватов,
    # без эпох, без редиректов.
    history = None
    deduped_live: list = []
    topic_result = TopicResult()
    redirects: list[RedirectInfo] = []
    drop_signals: list = []
    epoch_best: dict[int, "object"] = {}
    whois_reg_date = None
    whois_status_str: str | None = None

    try:
        # --- CDX ---
        # Cross-run кэш: если за последние ttl_hours тот же домен уже
        # сканировался, переиспользуем результат без обращения к IA.
        # Полезно когда оператор тестит настройки / перезапускает прогон.
        from webarhive.cdx.client import CdxRow
        from webarhive.db.repo import cdx_cache_get, cdx_cache_put
        cdx_cache_cfg = snapshot.get("cdx_cache", {}) or {}
        cdx_cache_enabled = bool(cdx_cache_cfg.get("enabled", True))
        cdx_cache_ttl_hours = int(cdx_cache_cfg.get("ttl_hours", 24))

        tracer.step("CDX", f"matchType={match_type}")
        cached = None
        if cdx_cache_enabled:
            async with session_factory() as s:
                cached = await cdx_cache_get(
                    s, domain_row.domain, match_type, cdx_cache_ttl_hours,
                )
        if cached is not None:
            rows_by_bucket, counts = cached
            # Defensive: старые записи кэша (до фикса header-row) могут
            # содержать строку-заголовок CDX в виде "строки". Отсеиваем
            # любую строку с нечисловым timestamp / литералом "urlkey".
            def _filter_cache_rows(rows: list[list[str]]) -> list[list[str]]:
                clean: list[list[str]] = []
                for r in rows:
                    if not r or len(r) < 2:
                        continue
                    if r[0] == "urlkey" or r[1] == "timestamp":
                        continue
                    if not (r[1] and r[1][:8].isdigit()):
                        continue
                    clean.append(r)
                return clean
            cdx_200 = [CdxRow.from_list(r) for r in _filter_cache_rows(rows_by_bucket.get("200", []))]
            cdx_3xx = [CdxRow.from_list(r) for r in _filter_cache_rows(rows_by_bucket.get("3xx", []))]
            cdx_404 = [CdxRow.from_list(r) for r in _filter_cache_rows(rows_by_bucket.get("404", []))]
            tracer.info(
                f"CDX-кэш HIT: {len(cdx_200)+len(cdx_3xx)+len(cdx_404)} "
                f"строк из кэша (TTL {cdx_cache_ttl_hours}ч)"
            )
        else:
            # Тянем три бакета (200/3xx/404) параллельно с серверной
            # фильтрацией: каждая CDX-страница возвращает только нужные
            # статусы, что на больших архивах (20k+ строк) экономит
            # ~40-60% трафика и парсинга. 5xx/other не анализируются.
            # 200-бакет дополнительно ограничен mimetype:text/html —
            # тематику строим только из HTML главной, поэтому feed/
            # wp-json/CSS/JSON-API не нужны и не должны раздувать payload.
            cdx_200, cdx_3xx, cdx_404 = await asyncio.gather(
                cdx.fetch_all(domain_row.domain, match_type=match_type,
                              filters=("statuscode:200", "mimetype:text/html")),
                cdx.fetch_all(domain_row.domain, match_type=match_type,
                              filters=("statuscode:3..",)),
                cdx.fetch_all(domain_row.domain, match_type=match_type,
                              filters=("statuscode:404",)),
            )
            if cdx_cache_enabled:
                # Сериализуем как массивы строк, идентично формату из
                # CDX API — чтобы при чтении восстановить через
                # CdxRow.from_list без дополнительной логики.
                rows_by_bucket = {
                    "200": [list(_cdx_to_list(r)) for r in cdx_200],
                    "3xx": [list(_cdx_to_list(r)) for r in cdx_3xx],
                    "404": [list(_cdx_to_list(r)) for r in cdx_404],
                }
                async with session_factory() as s:
                    await cdx_cache_put(
                        s, domain_row.domain, match_type,
                        rows_by_bucket=rows_by_bucket,
                        bucket_counts={
                            "200": len(cdx_200),
                            "3xx": len(cdx_3xx),
                            "404": len(cdx_404),
                        },
                    )
                    await s.commit()
        cdx_rows = cdx_200 + cdx_3xx + cdx_404
        # Sort by timestamp so downstream (history, gap detection) sees
        # the same chronological order as a single unfiltered fetch.
        cdx_rows.sort(key=lambda r: r.timestamp)
        buckets_total = {
            "200": len(cdx_200),
            "3xx": len(cdx_3xx),
            "404": len(cdx_404),
            "5xx": 0,
            "other": 0,
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

        # --- Topics: СТРОГО только главная страница ---
        # Анализируем тематику ТОЛЬКО по захватам главной (`/`,
        # `/index.html`). Внутренние URL (посты, теги, архивы,
        # wp-json, feeds) не репрезентируют «что это за домен» —
        # это разнотемные единицы внутри одного бренда. Раньше из
        # 17k версий 97% были не-главной → отсюда сплошные
        # «Пусто»/«Не определено».
        # Никакого fallback: если главная не сохранена, тематика
        # не классифицируется, домен помечается partial. Редиректы,
        # дропы и вердикт по флагам всё равно посчитаются.
        from webarhive.analysis.best_snapshot import _is_home_page
        home_only = [
            r for r in deduped_live
            if (r.mimetype or "").lower().startswith("text/html")
            and _is_home_page(r.original, domain_row.domain)
        ]
        if home_only and llm is not None:
            sorted_live = sorted(home_only, key=lambda r: r.timestamp)
            tracer.step(
                "ТЕМАТИКА",
                f"{len(sorted_live)} захватов главной "
                f"(из {len(deduped_live)} 200-снимков — остальные не главная)",
            )

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
                llm_parallelism=int(limits.get("llm_parallelism", 16)),
                progress=_topic_progress,
            )
            topic_partial = topic_result.partial
            tracer.topics_plan(
                total_versions=len(sorted_live),
                shift_points=len([v for v in topic_result.versions if v.classified]),
                llm_budget=limits["max_llm_calls_per_domain"],
            )
        elif home_only and llm is None:
            tracer.warn("LLM-клиент не подключён — тематика не классифицирована")
        elif deduped_live:
            # 200-захваты есть, но ни одного home-page → не можем
            # классифицировать. Помечаем partial, продолжаем редиректы/
            # дропы/вердикт по флагам.
            tracer.warn(
                f"тематика: главная страница не сохранена в архиве "
                f"({len(deduped_live)} 200-снимков, ни одного home-page) — "
                f"тематика пропущена, домен будет partial"
            )
            topic_partial = True

        # --- Redirects ---
        if history.redirect_rows:
            tracer.step("РЕДИРЕКТЫ", f"{len(history.redirect_rows)} 3xx-снапшотов")
            t0 = datetime.utcnow()

            async def _redir_progress(msg: str) -> None:
                tracer.info(msg)

            redir_cap = int(limits.get("redirect_cap", 150))
            redirects = await analyze_redirects(
                history.redirect_rows,
                source_domain=domain_row.domain,
                fetcher=fetcher,
                cap=redir_cap,
                progress=_redir_progress,
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
                review_cap = int(limits.get("redirect_llm_review_cap", 30))
                redirects = await llm_refine_redirects(
                    redirects,
                    source_topic=source_topic,
                    llm=llm,
                    model=models["redirect"],
                    fetcher=fetcher,
                    cdx=cdx,
                    review_cap=review_cap,
                    llm_parallelism=int(limits.get("llm_parallelism", 16)),
                    progress=_redir_progress,
                )
                tracer.info("LLM-уточнение редиректов выполнено")

        # --- Drops: feed gap-detection both 200-versions and 404 markers ---
        # 404 фильтруем до home-only по той же причине что и тематику:
        # 404 на отдельных блог-постах не значимы для «домен жил/умер».
        # Раньше gap-детекция видела 404 на /blog/2010-post вне диапазона
        # эпох главной → «тематику сравнить не вышло» в reason.
        gap_times: list[datetime] = [v.captured_at for v in topic_result.versions]
        from webarhive.analysis.best_snapshot import _is_home_page
        for row in history.not_found_rows:
            if not _is_home_page(row.original, domain_row.domain):
                continue
            ts = _safe_parse(row.timestamp)
            if ts:
                gap_times.append(ts)
        gaps = find_gaps(gap_times)
        drop_signals = score_drops(gaps, topic_result.epochs)
        if gaps:
            tracer.info(f"дропы: разрывов {len(gaps)}, оценено {sum(1 for d in drop_signals if d.is_drop)}")
        if roles.get("smart_drop") and llm is not None and drop_signals:
            async def _drop_progress(msg: str) -> None:
                tracer.info(msg)
            drop_signals = await smart_drop_assess(
                drop_signals,
                llm=llm,
                model=models["smart_drop"],
                llm_parallelism=int(limits.get("llm_parallelism", 16)),
                progress=_drop_progress,
            )
            tracer.info("LLM-оценка дропов выполнена")

        # --- Verdict ---
        if roles.get("verdict") and llm is not None:
            tracer.info("verdict: начинаю финальный анализ")
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
        bs_cfg = snapshot.get("best_snapshot", {}) or {}
        if bs_cfg.get("enabled") and topic_result.epochs:
            from webarhive.analysis.best_snapshot import (
                best_snapshot_for_epoch,
                epoch_candidates,
                filter_home_page_rows,
            )
            top_n = int(bs_cfg.get("top_n", 3))
            max_res = int(bs_cfg.get("max_resources_per_candidate", 8))
            per_epoch_timeout = float(bs_cfg.get("per_epoch_timeout_sec", 90))
            epoch_parallelism = max(1, int(bs_cfg.get("epoch_parallelism", 3)))
            min_epoch_days = int(bs_cfg.get("min_epoch_days", 30))
            max_epochs = max(1, int(bs_cfg.get("max_epochs", 10)))
            home_rows = filter_home_page_rows(cdx_rows, domain_row.domain)

            # Фильтр эпох: пропускаем короткие (title-shift в блоге часто
            # создаёт 30+ микро-эпох по неделе), плюс ограничиваем общее
            # число до max_epochs (берём самые длинные). Без этого best-
            # snap на блоге уходил в 25 минут timeout-ов.
            all_epochs = list(enumerate(topic_result.epochs))
            eligible = [
                (i, ep) for i, ep in all_epochs
                if (ep.period_to - ep.period_from).days >= min_epoch_days
            ]
            skipped_short = len(all_epochs) - len(eligible)
            if len(eligible) > max_epochs:
                eligible.sort(
                    key=lambda x: (x[1].period_to - x[1].period_from).days,
                    reverse=True,
                )
                eligible = eligible[:max_epochs]
                eligible.sort(key=lambda x: x[0])  # вернуть исходный порядок
            tracer.step(
                "ЛУЧШИЙ_СЛЕПОК",
                f"эпох всего: {len(all_epochs)}, обработаем: {len(eligible)} "
                f"(порог длительности: {min_epoch_days}д, "
                f"пропущено короткими: {skipped_short})",
            )

            async def _bs_progress(msg: str) -> None:
                tracer.info(msg)

            # Параллельные эпохи (скользящее окно). Общий IA throttle всё
            # равно лимитирует rate, но пока одна эпоха ждёт следующий
            # availability-чек, другая может уже фетчить HTML.
            bs_sem = asyncio.Semaphore(epoch_parallelism)

            async def _process_epoch(i: int, ep) -> None:
                cands = epoch_candidates(home_rows, ep.period_from, ep.period_to)
                if not cands:
                    tracer.info(f"эпоха #{i+1}: лучший слепок недоступен (нет 200-главной)")
                    return
                if http is None or throttle is None:
                    tracer.warn(f"эпоха #{i+1}: http/throttle не пробросаны — пропускаю best-snap")
                    return
                async with bs_sem:
                    try:
                        best = await asyncio.wait_for(
                            best_snapshot_for_epoch(
                                epoch_idx=i, candidates=cands,
                                source_domain=domain_row.domain,
                                fetcher=fetcher, cdx=cdx,
                                http=http, throttle=throttle,
                                top_n=top_n,
                                max_resources_per_candidate=max_res,
                                progress=_bs_progress,
                            ),
                            timeout=per_epoch_timeout,
                        )
                    except asyncio.TimeoutError:
                        tracer.warn(
                            f"эпоха #{i+1}: best-snap превысил {per_epoch_timeout:.0f}s — пропускаю"
                        )
                        return
                    except Exception as exc:
                        tracer.warn(f"эпоха #{i+1}: best-snap упал — {exc}")
                        return
                if best is not None:
                    epoch_best[i] = best
                    tracer.info(
                        f"эпоха #{i+1}: best {best.timestamp[:8]} · "
                        f"score {best.score} · ресурсов {best.resources_archived}/"
                        f"{best.resources_total}"
                    )

            await asyncio.gather(*(
                _process_epoch(i, ep) for i, ep in eligible
            ))

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
                # Сохраняем всё что успели посчитать ДО исключения, чтобы
                # карточка ERROR-домена не была пустой. Все поля могут быть
                # None если ошибка вылетела до их вычисления — тогда они
                # просто не перезаписываются.
                if history is not None:
                    d.first_capture_at = history.first_capture_at
                    d.last_capture_at = history.last_capture_at
                    d.age_days = history.age_days
                    d.total_captures = history.total_captures
                    d.total_versions = (
                        len(deduped_live) + len(history.redirect_rows)
                    )
                if whois_status_str is not None and d.whois_status is None:
                    d.whois_registration_date = whois_reg_date
                    d.whois_status = whois_status_str
                    d.whois_fetched_at = datetime.utcnow()
                # Эпохи / редиректы пишем только если их там ещё нет (на
                # success-пути они уже добавлены в основной persist-блок).
                from sqlalchemy import select as _select
                existing_epochs = (
                    await s.execute(_select(Epoch).where(Epoch.domain_id == d.id))
                ).scalars().first()
                if existing_epochs is None and topic_result.epochs:
                    for ep in topic_result.epochs:
                        s.add(Epoch(
                            domain_id=d.id,
                            period_from=ep.period_from,
                            period_to=ep.period_to,
                            category=ep.category,
                            confidence=ep.confidence,
                            reason=ep.reason,
                            sample_snapshot_url=ep.sample_snapshot_url,
                            versions_in_epoch=ep.versions_in_epoch,
                        ))
                existing_redirects = (
                    await s.execute(_select(Redirect).where(Redirect.domain_id == d.id))
                ).scalars().first()
                if existing_redirects is None and redirects:
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


def _cdx_to_list(r) -> tuple[str, str, str, str, str, str, str]:
    """Serialize a CdxRow back to the 7-field list form used by CDX API,
    so cdx_cache_put stores it in the exact format CdxRow.from_list reads
    back later."""
    return (r.urlkey, r.timestamp, r.original, r.mimetype,
            r.statuscode, r.digest, r.length)


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

    # HTTP/2 multiplexes many CDX / snapshot / availability calls through
    # one TCP connection (IA supports h2). Accept-Encoding is set
    # explicitly so we hit gzipped CDX responses + compressed snapshots.
    async with httpx.AsyncClient(
        timeout=60.0,
        headers={
            "User-Agent": "webarhive-checker/0.1 (+internal)",
            "Accept-Encoding": "gzip, deflate",
        },
        follow_redirects=False,
        http2=True,
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
        # Default raised to 1800s — раньше 600s стабильно срезал большие
        # архивы посреди фазы РЕДИРЕКТЫ или ТЕМАТИКА. Оператор может
        # снизить или поднять через /settings → per_domain_timeout_sec.
        per_domain_timeout = float(settings_snapshot.get("throttle", {}).get(
            "per_domain_timeout_sec", 1800))

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
                                http=http,
                                throttle=throttle,
                            ),
                            timeout=per_domain_timeout,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "domain %s exceeded %ss timeout — marking error",
                            d.domain, per_domain_timeout,
                        )
                        # Достаём последнюю строку live-flush'енной трассы,
                        # чтобы оператор видел, на какой стадии оборвалось.
                        async with session_factory() as s:
                            row = await s.get(Domain, d.id)
                            if row is not None and row.status not in (
                                DomainStatus.DONE.value,
                                DomainStatus.PARTIAL.value,
                            ):
                                # Парсим хвост трассы — последнюю «информативную» строку.
                                last_step = "?"
                                if row.trace:
                                    lines = [ln for ln in row.trace.splitlines() if ln.strip()]
                                    if lines:
                                        last_step = lines[-1][-160:]
                                row.status = DomainStatus.ERROR.value
                                row.error_message = (
                                    f"timeout {per_domain_timeout:.0f}s · оборвалось на: «{last_step}». "
                                    f"Если повторяется — поднимите per_domain_timeout_sec в /settings, "
                                    f"или снизьте redirect_cap / light_fetch_cap / отключите ENABLE_REDIRECT_LLM."
                                )
                                row.finished_at = datetime.utcnow()
                                # Дописываем явную строку в trace, чтобы в логе
                                # был след «здесь сработал per_domain_timeout».
                                row.trace = (row.trace or "") + (
                                    f"\n[{datetime.utcnow():%Y-%m-%d %H:%M:%S}] "
                                    f"[ERROR] >>> ОБОРВАНО по таймауту "
                                    f"{per_domain_timeout:.0f}s · последний шаг: {last_step}\n"
                                )
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
