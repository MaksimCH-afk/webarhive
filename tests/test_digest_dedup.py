"""Test that client-side digest dedup actually drops duplicates the
CDX server-side collapse couldn't catch (different URLs, same digest)."""

import pytest
import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webarhive.db import Base, Domain
from webarhive.db.models import DomainStatus


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sf
    await engine.dispose()


def _cdx_with_dup_digests():
    """5 different URLs, but only 2 unique digests — should dedup to 2."""
    pizza_html = (
        b"<html><head><title>Pizza</title></head><body>"
        b"<p>Pizza delivery</p></body></html>"
    )
    casino_html = (
        b"<html><head><title>Casino slots roulette bonus</title></head>"
        b"<body><p>Win big at casino</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "cdx/search/cdx" in url:
            body = [
                ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                # 3 URLs with the same pizza digest
                ["com,foo)/", "20200101000000", "http://foo.com/", "text/html", "200", "PIZZA_DIGEST", "100"],
                ["com,foo)/index.html", "20200102000000", "http://foo.com/index.html", "text/html", "200", "PIZZA_DIGEST", "100"],
                ["com,foo)/?utm=x", "20200103000000", "http://foo.com/?utm=x", "text/html", "200", "PIZZA_DIGEST", "100"],
                # 2 URLs with the same casino digest
                ["com,foo)/", "20230601000000", "http://foo.com/", "text/html", "200", "CASINO_DIGEST", "100"],
                ["com,foo)/index.html", "20230602000000", "http://foo.com/index.html", "text/html", "200", "CASINO_DIGEST", "100"],
            ]
            return httpx.Response(200, json=body)
        if "casino" in url.lower() or "/web/2023" in url:
            return httpx.Response(200, content=casino_html,
                                  headers={"content-type": "text/html"})
        if "/web/2020" in url:
            return httpx.Response(200, content=pizza_html,
                                  headers={"content-type": "text/html"})
        if "openrouter" in url:
            req_body = request.read().decode("utf-8")
            if "Casino" in req_body or "casino" in req_body:
                content = '{"category":"гемблинг_казино","confidence":0.95,"reason":"casino"}'
            else:
                content = '{"category":"коммерция_магазин","confidence":0.9,"reason":"pizza"}'
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_cost": 0.0001},
            })
        return httpx.Response(404)

    return handler


async def test_digest_dedup_drops_same_content_across_urls(session_factory):
    """5 snapshots (3 pizza + 2 casino, across different URLs) should
    collapse to 2 unique versions for topic analysis."""
    from webarhive.cdx.client import CdxClient
    from webarhive.cdx.throttle import IAThrottle
    from webarhive.db.repo import create_run, seed_domains
    from webarhive.fetcher.snapshot import SnapshotFetcher
    from webarhive.llm.client import OpenRouterClient
    from webarhive.orchestrator.runner import process_domain

    snap = {
        "models": {"classification": "m", "verdict": "m", "smart_drop": "m", "redirect": "m"},
        "roles": {"verdict": False, "smart_drop": False, "redirect_llm": False},
        "limits": {"max_llm_calls_per_domain": 10, "cost_budget_per_domain": 0.5,
                   "text_limit": 2000, "title_shift_threshold": 0,
                   "light_fetch_cap": 100},
        "throttle": {"concurrency": 1, "ia_rate_limit": 100, "ia_backoff": 0.01,
                     "ia_max_retries": 2},
        "input": {"check_subdomains": False},
        "whois": {"enabled": False},
        "best_snapshot": {"enabled": False},
    }

    async with session_factory() as s:
        run = await create_run(s, total=1, settings_snapshot=snap)
        rows = await seed_domains(s, run.id, ["foo.com"])
        await s.commit()
        domain_row = rows[0]
        run_id = run.id

    transport = httpx.MockTransport(_cdx_with_dup_digests())
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        throttle = IAThrottle(rate=100)
        cdx = CdxClient(throttle=throttle, client=http, backoff_base=0.01, max_retries=2)
        fetcher = SnapshotFetcher(throttle=throttle, client=http, backoff_base=0.01, max_retries=2)
        llm = OpenRouterClient(api_key="test-key", client=http, backoff_base=0.01, max_retries=2)

        await process_domain(
            domain_row=domain_row,
            run_id=run_id,
            snapshot=snap,
            session_factory=session_factory,
            cdx=cdx,
            fetcher=fetcher,
            llm=llm,
        )

    # Verify: trace mentions dedup; final total_versions reflects
    # deduped count (2 unique digests, no redirects → total_versions=2).
    async with session_factory() as s:
        d = await s.get(Domain, domain_row.id)
        assert d is not None
        # 5 raw 200-snapshots → 2 unique digests
        assert "digest-дедуп: 200-снапшотов 5 → 2" in (d.trace or ""), \
            f"trace missing dedup line; got:\n{d.trace}"
        assert d.total_versions == 2, f"expected 2 versions, got {d.total_versions}"
        assert d.status in (DomainStatus.DONE.value, DomainStatus.PARTIAL.value)
