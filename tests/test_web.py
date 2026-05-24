"""Smoke tests for the FastAPI dashboard.

Uses ASGI in-memory transport via httpx so no real port is opened.
The DB engine is patched to a per-test SQLite file so the app's
get_session resolves cleanly.
"""

import os
import tempfile
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def fresh_db_url(monkeypatch):
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    url = f"sqlite+aiosqlite:///{tmp}"
    monkeypatch.setenv("DATABASE_URL", url)
    # Reset cached engine + settings
    from webarhive.config.settings import get_settings as gs
    gs.cache_clear()
    from webarhive.db import engine as eng
    eng._engine = None
    eng._session_factory = None
    yield url


@pytest.fixture
async def app(fresh_db_url):
    from webarhive.db.engine import create_engine_and_session
    from webarhive.db.models import Base

    engine, _ = create_engine_and_session(database_url=fresh_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    # Force module singletons to use this URL
    from webarhive.db import engine as eng
    eng._engine = None
    eng._session_factory = None

    from webarhive.web import create_app
    return create_app()


async def test_main_page_empty_state(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "Прогонов ещё нет" in r.text


async def test_help_page_renders(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/help")
        assert r.status_code == 200
        assert "Возраст" in r.text
        assert "Лента эпох" in r.text


async def test_settings_page_renders(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/settings")
        assert r.status_code == 200
        assert "APP_DOMAIN" in r.text


async def test_upload_text_returns_report(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/upload", data={"text": "foo.com\nblog.bar.co.uk\ngarbage\n"})
        assert r.status_code == 200
        body = r.json()
        assert body["raw_lines"] == 3
        assert "foo.com" in body["valid_unique"]
        assert "bar.co.uk" in body["valid_unique"]  # PSL trim
        assert len(body["rejected"]) == 1
