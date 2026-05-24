"""Thin async repository helpers used by orchestrator & web layer.

Not a full DAO — just the few high-traffic queries that matter for
keeping `runs` view, the canvas, and the card page fast.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webarhive.db.models import (
    Domain,
    DomainStatus,
    Drop,
    Epoch,
    LlmCall,
    Redirect,
    Run,
    RunStatus,
    Verdict,
)

# ----- runs -----

async def create_run(session: AsyncSession, *, total: int, settings_snapshot: dict, note: str | None = None) -> Run:
    run = Run(
        total_domains=total,
        settings_snapshot=settings_snapshot,
        note=note,
        status=RunStatus.RUNNING.value,
    )
    session.add(run)
    await session.flush()
    return run


async def list_runs(session: AsyncSession, *, limit: int = 100) -> list[Run]:
    result = await session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))
    return list(result.scalars())


async def get_run(session: AsyncSession, run_id: int) -> Run | None:
    return await session.get(Run, run_id)


async def finish_run(session: AsyncSession, run_id: int, *, status: RunStatus) -> None:
    await session.execute(
        update(Run)
        .where(Run.id == run_id)
        .values(status=status.value, finished_at=datetime.utcnow())
    )


# ----- domains -----

async def seed_domains(session: AsyncSession, run_id: int, domains: Iterable[str]) -> list[Domain]:
    """Insert pending Domain rows. Idempotent within a run."""
    inserted: list[Domain] = []
    for name in domains:
        d = Domain(run_id=run_id, domain=name, status=DomainStatus.PENDING.value)
        session.add(d)
        inserted.append(d)
    await session.flush()
    return inserted


async def list_domains(session: AsyncSession, run_id: int) -> list[Domain]:
    res = await session.execute(
        select(Domain).where(Domain.run_id == run_id).order_by(Domain.id)
    )
    return list(res.scalars())


async def get_domain(session: AsyncSession, domain_id: int) -> Domain | None:
    return await session.get(Domain, domain_id)


async def get_pending_for_run(session: AsyncSession, run_id: int) -> list[Domain]:
    """Resumability (spec §2.2): pick up not-yet-done rows on restart."""
    res = await session.execute(
        select(Domain).where(
            Domain.run_id == run_id,
            Domain.status.in_([DomainStatus.PENDING.value, DomainStatus.RUNNING.value]),
        )
    )
    return list(res.scalars())


async def increment_run_counters(
    session: AsyncSession, run_id: int, *, verdict: Verdict | None, error: bool
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.processed_domains += 1
    if error:
        run.error_count += 1
    elif verdict is Verdict.CLEAN:
        run.clean_count += 1
    elif verdict is Verdict.NUANCED:
        run.nuanced_count += 1
    elif verdict is Verdict.DIRTY:
        run.dirty_count += 1


# ----- llm audit -----

def _gzip_text(text: str | None, limit: int) -> bytes | None:
    if text is None:
        return None
    truncated = text[:limit]
    return gzip.compress(truncated.encode("utf-8"))


def unzip_text(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return gzip.decompress(blob).decode("utf-8", errors="replace")


async def record_llm_call(
    session: AsyncSession,
    *,
    run_id: int,
    domain_id: int | None,
    role: str,
    model: str,
    snapshot_url: str | None,
    input_text: str | None,
    text_limit: int,
    output: dict | None,
    raw_output: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: float | None,
    latency_ms: int | None,
    error: str | None = None,
) -> LlmCall:
    call = LlmCall(
        run_id=run_id,
        domain_id=domain_id,
        role=role,
        model=model,
        snapshot_url=snapshot_url,
        input_text_gz=_gzip_text(input_text, text_limit),
        output=output,
        raw_output=raw_output,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        error=error,
    )
    session.add(call)
    await session.flush()
    return call


# ----- bulk write of result components -----

async def write_epochs(session: AsyncSession, domain_id: int, epochs: Iterable[Epoch]) -> None:
    for e in epochs:
        e.domain_id = domain_id
        session.add(e)


async def write_redirects(session: AsyncSession, domain_id: int, redirects: Iterable[Redirect]) -> None:
    for r in redirects:
        r.domain_id = domain_id
        session.add(r)


async def write_drops(session: AsyncSession, domain_id: int, drops: Iterable[Drop]) -> None:
    for d in drops:
        d.domain_id = domain_id
        session.add(d)
