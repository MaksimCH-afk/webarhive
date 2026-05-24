"""HTTP routes — HTML pages + JSON/SSE API.

Dashboard structure (spec §15):
- 1.  / — runs list + launch
- 1a. /runs/{id}/stream — SSE progress
- 2.  /runs/{id} — domain canvas
- 3.  /domains/{id} — domain card
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy import desc, select

from webarhive.config import get_settings
from webarhive.db.engine import get_session
from webarhive.db.models import Domain, DomainStatus, Drop, Epoch, LlmCall, Redirect, Run, RunStatus
from webarhive.db.repo import unzip_text
from webarhive.domains.loader import load_from_bytes, load_from_text
from webarhive.orchestrator.runner import run_pipeline, start_run
from webarhive.web.deps import get_templates

html_router = APIRouter()
api_router = APIRouter(prefix="/api")


# ----- HTML -----

@html_router.get("/", response_class=HTMLResponse)
async def main_page(request: Request):
    async with get_session() as s:
        runs = (await s.execute(select(Run).order_by(desc(Run.started_at)).limit(100))).scalars().all()
    return get_templates(request).TemplateResponse(
        request,
        "main.html",
        {"runs": runs, "settings": get_settings()},
    )


@html_router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_canvas(
    request: Request,
    run_id: int,
    verdict: str | None = Query(None),
    risky_only: bool = Query(False),
    review_only: bool = Query(False),
    sort: str = Query("id"),
):
    async with get_session() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        stmt = select(Domain).where(Domain.run_id == run_id)
        if verdict:
            stmt = stmt.where(Domain.verdict == verdict)
        if risky_only:
            stmt = stmt.where(Domain.risky_flag_count > 0)
        if review_only:
            stmt = stmt.where(Domain.review_flag_count > 0)
        sort_map = {
            "age": Domain.age_days.desc().nullslast(),
            "verdict": Domain.verdict.asc().nullslast(),
            "flags": (Domain.risky_flag_count + Domain.review_flag_count).desc(),
            "id": Domain.id.asc(),
        }
        stmt = stmt.order_by(sort_map.get(sort, Domain.id.asc()))
        domains = (await s.execute(stmt)).scalars().all()
    return get_templates(request).TemplateResponse(
        request,
        "canvas.html",
        {
            "run": run,
            "domains": domains,
            "filters": {
                "verdict": verdict, "risky_only": risky_only,
                "review_only": review_only, "sort": sort,
            },
        },
    )


@html_router.get("/domains/{domain_id}", response_class=HTMLResponse)
async def domain_card(request: Request, domain_id: int):
    async with get_session() as s:
        d = await s.get(Domain, domain_id)
        if d is None:
            raise HTTPException(404)
        epochs = (await s.execute(select(Epoch).where(Epoch.domain_id == domain_id)
                                  .order_by(Epoch.period_from))).scalars().all()
        redirects = (await s.execute(select(Redirect).where(Redirect.domain_id == domain_id)
                                     .order_by(Redirect.captured_at))).scalars().all()
        drops = (await s.execute(select(Drop).where(Drop.domain_id == domain_id)
                                 .order_by(Drop.gap_from))).scalars().all()
        llm_calls = (await s.execute(select(LlmCall).where(LlmCall.domain_id == domain_id)
                                     .order_by(LlmCall.created_at))).scalars().all()
        run = await s.get(Run, d.run_id)
    return get_templates(request).TemplateResponse(
        request,
        "card.html",
        {
            "domain": d, "epochs": epochs,
            "redirects": redirects, "drops": drops, "llm_calls": llm_calls,
            "run": run,
        },
    )


@html_router.get("/domains/{domain_id}/trace.txt", response_class=PlainTextResponse)
async def domain_trace(domain_id: int):
    async with get_session() as s:
        d = await s.get(Domain, domain_id)
        if d is None:
            raise HTTPException(404)
    headers = {"Content-Disposition": f'attachment; filename="trace_{d.domain}.txt"'}
    return PlainTextResponse(d.trace or "(пусто)", headers=headers)


@html_router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return get_templates(request).TemplateResponse(request, "help.html", {})


@html_router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return get_templates(request).TemplateResponse(
        request, "settings.html", {"settings": get_settings()}
    )


# ----- API: upload + launch -----

@api_router.post("/upload")
async def api_upload(
    request: Request,
    text: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
):
    settings = get_settings()
    if file is not None and file.filename:
        data = await file.read()
        report = load_from_bytes(file.filename, data,
                                 check_subdomains=settings.check_subdomains)
    else:
        report = load_from_text(text, check_subdomains=settings.check_subdomains)
    return JSONResponse({
        "raw_lines": report.raw_lines,
        "valid_unique": report.valid_unique,
        "dropped": report.dropped,
        "rejected": [{"original": r.original, "reason": r.reason}
                     for r in report.rejected],
    })


@api_router.post("/runs")
async def api_create_run(
    request: Request,
    domains: Annotated[str, Form()],  # newline-separated
    note: Annotated[str, Form()] = "",
):
    settings = get_settings()
    names = [ln.strip() for ln in domains.splitlines() if ln.strip()]
    if not names:
        raise HTTPException(400, "no domains")
    snap = settings.snapshot()
    run_id = await start_run(domains=names, settings_snapshot=snap, note=note or None)

    # Kick off the pipeline in the background.
    task = asyncio.create_task(run_pipeline(
        run_id=run_id, settings_snapshot=snap, api_key=settings.openrouter_api_key,
    ))
    request.app.state.pipeline_tasks[run_id] = task
    return JSONResponse({"run_id": run_id, "redirect": f"/runs/{run_id}"})


@api_router.get("/runs/{run_id}/stream")
async def api_run_stream(run_id: int):
    """SSE — push run + per-domain progress every second until done."""

    async def gen():
        last_state = None
        while True:
            async with get_session() as s:
                run = await s.get(Run, run_id)
                if run is None:
                    yield "event: error\ndata: not_found\n\n"
                    return
                state = {
                    "status": run.status,
                    "processed": run.processed_domains,
                    "total": run.total_domains,
                    "clean": run.clean_count,
                    "nuanced": run.nuanced_count,
                    "dirty": run.dirty_count,
                    "errors": run.error_count,
                }
                # Show "current" — pick latest running domain if any.
                running = (await s.execute(
                    select(Domain.domain).where(
                        Domain.run_id == run_id,
                        Domain.status == DomainStatus.RUNNING.value,
                    ).limit(1)
                )).scalar_one_or_none()
                state["current"] = running

            if state != last_state:
                yield f"data: {json.dumps(state, ensure_ascii=False)}\n\n"
                last_state = state
            if state["status"] in (RunStatus.FINISHED.value, RunStatus.ABORTED.value,
                                   RunStatus.ERROR.value) and state["processed"] >= state["total"]:
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@api_router.post("/runs/{run_id}/abort")
async def api_abort_run(request: Request, run_id: int):
    task = request.app.state.pipeline_tasks.pop(run_id, None)
    if task is not None and not task.done():
        task.cancel()
    async with get_session() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise HTTPException(404)
        run.status = RunStatus.ABORTED.value
        await s.commit()
    return JSONResponse({"status": "aborted"})


@api_router.get("/runs/{run_id}/export.csv", response_class=PlainTextResponse)
async def api_export_csv(
    run_id: int,
    verdict: str | None = Query(None),
    risky_only: bool = Query(False),
    review_only: bool = Query(False),
):
    """Export current filtered selection as CSV (spec §15 экран 2)."""
    import csv
    import io

    async with get_session() as s:
        stmt = select(Domain).where(Domain.run_id == run_id)
        if verdict:
            stmt = stmt.where(Domain.verdict == verdict)
        if risky_only:
            stmt = stmt.where(Domain.risky_flag_count > 0)
        if review_only:
            stmt = stmt.where(Domain.review_flag_count > 0)
        domains = (await s.execute(stmt.order_by(Domain.id))).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "domain", "verdict", "age_days", "first_capture", "last_capture",
        "epochs", "risky_flags", "review_flags", "status", "partial",
    ])
    for d in domains:
        w.writerow([
            d.domain, d.verdict or "", d.age_days or "",
            d.first_capture_at.isoformat() if d.first_capture_at else "",
            d.last_capture_at.isoformat() if d.last_capture_at else "",
            "",  # epochs count needs join; left blank in fast export
            d.risky_flag_count, d.review_flag_count, d.status,
            "1" if d.status == DomainStatus.PARTIAL.value else "0",
        ])
    return PlainTextResponse(
        buf.getvalue(),
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}.csv"',
                 "Content-Type": "text/csv; charset=utf-8"},
    )


@api_router.get("/llm_calls/{call_id}/input.txt", response_class=PlainTextResponse)
async def api_llm_input(call_id: int):
    async with get_session() as s:
        call = await s.get(LlmCall, call_id)
        if call is None:
            raise HTTPException(404)
    return PlainTextResponse(unzip_text(call.input_text_gz) or "(пусто)")
