"""SQLAlchemy engine and async-session factories.

The factories are deliberately lazy: importing the application must not require a
configured database URL or open a network connection.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def get_database_url(database_url: str | None = None) -> str:
    """Return a configured database URL or raise a clear configuration error."""

    value = database_url or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError(
            "DATABASE_URL is required, for example "
            "postgresql://user:password@localhost:5432/epi_evidence"
        )
    return value


def normalize_database_url(database_url: str) -> str:
    """Use psycopg 3's async SQLAlchemy dialect for PostgreSQL URLs."""

    if database_url.startswith("postgresql+psycopg2://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql+psycopg2://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def build_async_engine(
    database_url: str | None = None,
    **engine_kwargs: object,
) -> AsyncEngine:
    """Build an async engine without creating tables or running migrations."""

    options = {"pool_pre_ping": True, **engine_kwargs}
    return create_async_engine(
        normalize_database_url(get_database_url(database_url)),
        **options,
    )


def build_session_factory(
    database_url: str | None = None,
    **engine_kwargs: object,
) -> async_sessionmaker[AsyncSession]:
    """Build an ``AsyncSession`` factory bound to a newly created engine."""

    engine = build_async_engine(database_url, **engine_kwargs)
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session and commit or roll back one application unit of work."""

    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

