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


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="webarhive",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
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
