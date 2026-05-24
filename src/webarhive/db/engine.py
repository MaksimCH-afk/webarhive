"""Async engine + session factory.

SQLite by default (zero-admin), Postgres via DATABASE_URL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from webarhive.config import get_settings


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite"):
        # sqlite+aiosqlite:///./data/webarhive.db → path after ///
        _, _, path_part = url.partition("///")
        path_part = path_part.split("?", 1)[0]
        if not path_part or path_part == ":memory:":
            return
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def create_engine_and_session(
    database_url: str | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = database_url or get_settings().database_url
    _ensure_sqlite_dir(url)
    engine = create_async_engine(url, future=True, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


# Module-level lazy singleton for app code.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _factory() -> async_sessionmaker[AsyncSession]:
    global _engine, _session_factory
    if _session_factory is None:
        _engine, _session_factory = create_engine_and_session()
    return _session_factory


@asynccontextmanager
async def get_session():
    sf = _factory()
    async with sf() as session:
        yield session


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
