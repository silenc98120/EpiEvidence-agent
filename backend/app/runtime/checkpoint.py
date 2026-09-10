"""LangGraph checkpoint 的应用级构造边界。

PostgreSQL saver 是可选运行时依赖，导入业务模块时不连接数据库；应用启动层
负责进入返回的异步上下文并把 saver 注入 ``build_evidence_graph``。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any


@asynccontextmanager
async def build_postgres_checkpointer(
    database_url: str,
    *,
    setup: bool = True,
) -> AsyncIterator[Any]:
    """创建 LangGraph 官方 PostgreSQL checkpointer。

    ``langgraph-checkpoint-postgres`` 未安装时抛出明确错误；不会静默回退到内存，
    因为调用方选择该工厂即表示需要进程重启后的恢复能力。
    """

    if not database_url or not database_url.strip():
        raise ValueError("database_url 不能为空")
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - depends on deployment extra
        raise RuntimeError(
            "PostgreSQL checkpoint 需要安装 langgraph-checkpoint-postgres。"
        ) from exc

    async with AsyncPostgresSaver.from_conn_string(database_url.strip()) as saver:
        if setup:
            await saver.setup()
        yield saver
