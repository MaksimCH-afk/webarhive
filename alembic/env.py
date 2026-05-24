"""Alembic env: sync mode, picks DATABASE_URL from env if present."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from webarhive.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# Override the URL from .env if provided. For Alembic we want sync driver,
# so coerce sqlite+aiosqlite → sqlite, postgresql+asyncpg → postgresql.
env_url = os.environ.get("DATABASE_URL")
if env_url:
    env_url = env_url.replace("+aiosqlite", "").replace("+asyncpg", "")
    config.set_main_option("sqlalchemy.url", env_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
