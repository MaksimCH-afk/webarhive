import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webarhive.db import Base
from webarhive.db.models import Verdict
from webarhive.db.repo import (
    create_run,
    increment_run_counters,
    record_llm_call,
    seed_domains,
    unzip_text,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sm() as s:
        yield s
    await engine.dispose()


async def test_run_create_and_count(session):
    run = await create_run(
        session,
        total=3,
        settings_snapshot={"models": {"classification": "x"}},
    )
    await seed_domains(session, run.id, ["foo.com", "bar.com", "baz.com"])
    await session.commit()
    assert run.total_domains == 3

    await increment_run_counters(session, run.id, verdict=Verdict.CLEAN, error=False)
    await increment_run_counters(session, run.id, verdict=Verdict.DIRTY, error=False)
    await increment_run_counters(session, run.id, verdict=None, error=True)
    await session.commit()
    await session.refresh(run)
    assert run.processed_domains == 3
    assert run.clean_count == 1
    assert run.dirty_count == 1
    assert run.error_count == 1


async def test_llm_call_gzip_input_text(session):
    run = await create_run(session, total=1, settings_snapshot={})
    long_text = "hello " * 1000  # 6000 chars
    call = await record_llm_call(
        session,
        run_id=run.id,
        domain_id=None,
        role="classification",
        model="test/model",
        snapshot_url="http://web.archive.org/web/20200101/foo.com",
        input_text=long_text,
        text_limit=2000,
        output={"category": "не_определено", "confidence": 0.0, "reason": "test"},
        raw_output=None,
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.0001,
        latency_ms=120,
    )
    await session.commit()
    decoded = unzip_text(call.input_text_gz)
    # Truncated to text_limit
    assert decoded is not None and len(decoded) == 2000
    # Compressed payload is much smaller than original input
    assert len(call.input_text_gz) < 500
