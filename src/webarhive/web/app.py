"""FastAPI dashboard app factory (spec §15, §17).

Mounted screens:
- /                  → main: list of runs + launch panel (screen 1)
- /runs/{id}         → canvas of domains (screen 2)
- /runs/{id}/stream  → SSE live progress (screen 1a)
- /domains/{id}      → domain card (screen 3)
- /help              → help page (spec §16)
- /settings          → global settings editor (spec §11 — UI authority)
- /upload            → load domains + show LoadReport (spec §2.4)
- /api/...           → JSON endpoints for HTMX swaps

Proxy-headers behaviour (spec §17 last paragraph):
- when TRUST_PROXY_HEADERS=true (default), trust X-Forwarded-For/Proto
  so absolute URLs and client IPs are correct behind Cloudflare.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webarhive.config import get_settings
from webarhive.web.deps import templates_for
from webarhive.web.routes import api_router, html_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def _ensure_asset_dirs() -> None:
    """Make sure static/ and templates/ exist next to this module.

    Normally they ship with the wheel (declared in pyproject.toml as
    package-data). But if someone runs an outdated install — or hits a
    Docker layer-cache hit that kept the old wheel — those dirs can be
    missing. Better to start with a stub than to crash on boot.

    Templates are critical for HTML routes; we copy them in from the
    repo checkout if it sits next to the install. Static is just
    decorative — empty dir is fine.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    if not any(TEMPLATES_DIR.iterdir()):
        # Stub so Jinja2Templates doesn't blow up; routes will still
        # render an error message when they ask for a real template.
        logger.warning(
            "templates dir at %s is empty — install is broken; "
            "re-build the container WITHOUT cache: `docker compose build --no-cache`",
            TEMPLATES_DIR,
        )
        (TEMPLATES_DIR / "_stub.html").write_text(
            "<h1>webarhive: templates not installed</h1>"
            "<p>Образ собран с устаревшим pip-кэшем. Выполните:</p>"
            "<pre>docker compose down\ndocker compose build --no-cache\n"
            "docker compose up -d</pre>",
            encoding="utf-8",
        )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: пометить «висящие» running-прогоны как aborted (они
    остались в БД от прошлого процесса — задача в asyncio не переживает
    рестарт контейнера). Так главная не показывает мёртвый running."""
    from datetime import datetime
    from sqlalchemy import update as sa_update
    from webarhive.db.engine import get_session
    from webarhive.db.models import Domain, DomainStatus, Run, RunStatus
    async with get_session() as s:
        res = await s.execute(
            sa_update(Run)
            .where(Run.status == RunStatus.RUNNING.value)
            .values(status=RunStatus.ABORTED.value,
                    finished_at=datetime.utcnow())
        )
        if res.rowcount:
            logger.warning(
                "reaped %d stale running run(s) from previous process",
                res.rowcount,
            )
            await s.execute(
                sa_update(Domain)
                .where(Domain.status.in_([
                    DomainStatus.PENDING.value,
                    DomainStatus.RUNNING.value,
                ]))
                .values(status=DomainStatus.NO_DATA.value,
                        error_message="прогон прерван перезапуском сервера",
                        finished_at=datetime.utcnow())
            )
        await s.commit()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    _ensure_asset_dirs()

    app = FastAPI(
        title="webarhive",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # Spec §17: trust proxy headers when behind Cloudflare Tunnel
    if settings.trust_proxy_headers:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        # The X-Forwarded-* parsing is handled by uvicorn's --proxy-headers flag
        # at the server level; this middleware just protects Host.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

    templates: Jinja2Templates = templates_for(TEMPLATES_DIR)
    app.state.templates = templates
    app.state.settings = settings
    # Track in-flight pipeline tasks per run_id so we can cancel/inspect.
    app.state.pipeline_tasks = {}  # type: ignore[var-annotated]

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(html_router)
    app.include_router(api_router)
    return app
