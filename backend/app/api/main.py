"""本地研究工作台的 FastAPI 应用工厂。"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv

load_dotenv(verbose=True)

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from fastapi import FastAPI

from backend.agent.graph import build_application_graph
from backend.app.api.routes import create_router
from backend.app.api.services.event_broker import EventBroker
from backend.app.db import ManagedSessionFactory, ResearchTaskRepository
from backend.app.observability import LangfuseRuntime
from backend.app.runtime import TaskCoordinator
from backend.app.api.services.task_store import TaskStore

def _mask_langfuse_payload(*, data: Any) -> Any:
    """保留研究输入输出，移除内部 system prompt。"""

    if isinstance(data, list):
        return [_mask_langfuse_payload(data=item) for item in data]

    if not isinstance(data, dict):
        return data

    masked = {
        key: _mask_langfuse_payload(data=value)
        for key, value in data.items()
    }
    if data.get("role") == "system" or data.get("type") == "system":
        if "content" in masked:
            masked["content"] = "[已省略内部 system prompt]"
    return masked

def _create_langfuse_client() -> Langfuse | None:
    if not (
        os.getenv("LANGFUSE_PUBLIC_KEY")
        and os.getenv("LANGFUSE_SECRET_KEY")
    ):
        return None

    return Langfuse(
        environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "local"),
        mask=_mask_langfuse_payload,
    )


def create_langfuse_callback_handler(
    langfuse: Langfuse | None,
) -> CallbackHandler | None:
    """为完整主图创建复用当前 Langfuse 客户端的 LangChain callback。"""

    if langfuse is None:
        return None

    return CallbackHandler(public_key=os.environ["LANGFUSE_PUBLIC_KEY"])


def create_langfuse_runtime(langfuse: Langfuse | None) -> LangfuseRuntime:
    """创建传给主图每个节点的共享 Langfuse 运行时依赖。"""

    return LangfuseRuntime(
        client=langfuse,
        callback_handler=create_langfuse_callback_handler(langfuse),
    )


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    """在应用退出前导出尚未批量发送的 Langfuse observation。"""

    try:
        yield
    finally:
        langfuse = getattr(app.state, "langfuse", None)
        if langfuse is not None:
            langfuse.flush()
        session_factory = getattr(app.state, "database_session_factory", None)
        if session_factory is not None:
            await session_factory.dispose()

def create_app(
    graph: Any | None = None,
    *,
    store: Any | None = None,
    coordinator: TaskCoordinator | None = None,
    graph_builder: Callable[[LangfuseRuntime], Any] | None = None,
    task_persistence: Any | None = None,
    database_session_factory: ManagedSessionFactory | None = None,
) -> FastAPI:
    """创建一个带有可注入的 graph、task store 和 task coordinator 的应用。

    Args:
        ``store`` 可以是内存中的 ``TaskStore``，也可以是基于 Redis 的实现；
        ``coordinator`` 是可注入的，以便部署代码可以提供使用 PostgreSQL LangGraph 检查点编译的 graph；
        ``graph_builder`` 在 Langfuse client 初始化后接收共享运行时依赖，用于构造
        带有节点观测与 LangChain tracing 的完整主图。
        ``task_persistence`` 保存跨 API、图与 Celery Worker 共用的任务事实；默认测试
        应用可以省略它，完整默认应用必须提供。
    """

    if graph_builder is not None and (graph is not None or coordinator is not None):
        raise ValueError("graph_builder 不能与 graph 或 coordinator 同时提供")

    store = store or TaskStore()
    broker = EventBroker()
    langfuse = _create_langfuse_client()
    langfuse_runtime = create_langfuse_runtime(langfuse)
    if graph_builder is not None:
        graph = graph_builder(langfuse_runtime)
    task_coordinator = coordinator or TaskCoordinator(
        graph,
        store,
        broker,
        langfuse=langfuse,
        task_persistence=task_persistence,
    )
    app = FastAPI(
        title="EpiEvidence Research API",
        lifespan=_application_lifespan,
    )
    app.include_router(create_router(store, task_coordinator, broker))
    app.state.task_store = store
    app.state.event_broker = broker
    app.state.compiled_graph = graph
    app.state.task_coordinator = task_coordinator
    app.state.langfuse = langfuse
    app.state.langfuse_callback_handler = langfuse_runtime.callback_handler
    app.state.langfuse_runtime = langfuse_runtime
    app.state.task_persistence = task_persistence
    app.state.database_session_factory = database_session_factory
    return app


def _create_default_app() -> FastAPI:
    """创建共享 PostgreSQL 生命周期的默认完整研究应用。"""

    session_factory = ManagedSessionFactory()
    return create_app(
        graph_builder=lambda runtime: build_application_graph(
            runtime,
            session_factory=session_factory,
        ),
        task_persistence=ResearchTaskRepository(session_factory),
        database_session_factory=session_factory,
    )


app = _create_default_app()
