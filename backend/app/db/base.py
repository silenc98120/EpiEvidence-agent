from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    """Base class for all database ORM models."""

async def create_all_tables(engine: AsyncEngine) -> None:
    """根据已注册的 ORM 模型创建不存在的数据库表。"""

    # 触发所有 ORM 模型导入，使它们注册到 Base.metadata。
    from . import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)