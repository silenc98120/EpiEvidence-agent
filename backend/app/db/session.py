"""SQLAlchemy engine and async-session factories.

The factories are deliberately lazy: importing the application must not require a
configured database URL or open a network connection.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

load_dotenv()

def get_database_url(database_url: str | None = None) -> str:
    """Return a configured database URL or raise a clear configuration error."""

    value = database_url or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError(
            "DATABASE_URL is required, for example "
            "postgresql://user:password@localhost:5432/epi_evidence_db"
        )
    return value


def normalize_database_url(database_url: str) -> str:
    """Use psycopg 3's async SQLAlchemy dialect for PostgreSQL URLs."""

    if database_url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + database_url.removeprefix(
            "postgresql+asyncpg://"
        )
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


class ManagedSessionFactory:
    """惰性创建并统一释放应用进程使用的数据库连接池。"""

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    def __call__(self) -> AsyncSession:
        if self._session_factory is None:
            self._engine = build_async_engine(self._database_url)
            self._session_factory = async_sessionmaker(
                self._engine,
                expire_on_commit=False,
                class_=AsyncSession,
            )
        return self._session_factory()

    async def dispose(self) -> None:
        """在应用关闭时释放已创建的连接池。"""

        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


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

