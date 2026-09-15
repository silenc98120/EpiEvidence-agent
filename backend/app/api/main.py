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
from fastapi.middleware.cors import CORSMiddleware

from backend.agent.graph import build_application_graph, build_intent_recognizer, model
from backend.app.api.routes import create_router
from backend.app.api.openai_compat import create_openai_router
from backend.app.api.services.event_broker import EventBroker
from backend.app.db import ManagedSessionFactory, ResearchTaskRepository
from backend.app.db.repositories import ConversationRepository
from backend.app.observability import LangfuseRuntime
from backend.app.runtime import (
    TaskCoordinator,
    build_postgres_checkpointer,
    configure_checkpoint_event_loop_policy,
)
from backend.app.api.services.task_store import TaskStore
from backend.app.api.services.conversation_store import ConversationStore


# psycopg async 无法运行在 Windows 默认 ProactorEventLoop；必须在 Uvicorn 建立
# 应用事件循环前完成切换，才能使用 PostgreSQL LangGraph checkpoint。
configure_checkpoint_event_loop_policy()

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


async def _simple_chat_response(query: str) -> str:
    """研究图外的轻量 Chatbot；不读取或写入文献研究状态。"""
    response = await model.ainvoke(
        "你是 EpiEvidence 的简短对话助手。友好回答用户；如对方尚未提出文献研究问题，"
        "提醒其可提供疾病、干预、人群或结局。不要提供诊断或治疗建议。\n\n用户：" + query
    )
    return str(response.content).strip()


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    """在应用退出前导出尚未批量发送的 Langfuse observation。"""

    try:
        checkpoint_graph_builder = getattr(
            app.state,
            "checkpoint_graph_builder",
            None,
        )
        if checkpoint_graph_builder is None:
            await _prepare_embedding_resource(app, app.state.compiled_graph)
            yield
        else:
            checkpoint_database_url = str(
                getattr(app.state, "checkpoint_database_url", "") or ""
            ).strip()
            if not checkpoint_database_url:
                raise RuntimeError(
                    "CHECKPOINTS_DATABASE_URL 未配置，无法启用 PostgreSQL checkpoint。"
                )
            checkpoint_factory = app.state.checkpoint_factory
            async with checkpoint_factory(checkpoint_database_url) as checkpointer:
                graph = checkpoint_graph_builder(
                    app.state.langfuse_runtime,
                    checkpointer,
                )
                app.state.compiled_graph = graph
                app.state.task_coordinator.set_graph(graph)
                await _prepare_embedding_resource(app, graph)
                yield
    finally:
        embedding_client = getattr(app.state, "embedding_client", None)
        close = getattr(embedding_client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        langfuse = getattr(app.state, "langfuse", None)
        if langfuse is not None:
            langfuse.flush()
        session_factory = getattr(app.state, "database_session_factory", None)
        if session_factory is not None:
            await session_factory.dispose()


async def _prepare_embedding_resource(app: FastAPI, graph: Any) -> None:
    """在接收请求前加载图持有的本地 embedding 模型。"""

    embedding_client = getattr(graph, "_embedding_client_resource", None)
    app.state.embedding_client = embedding_client
    load = getattr(embedding_client, "load", None)
    if load is not None:
        result = load()
        if hasattr(result, "__await__"):
            await result

def create_app(
    graph: Any | None = None,
    *,
    store: Any | None = None,
    coordinator: TaskCoordinator | None = None,
    graph_builder: Callable[[LangfuseRuntime], Any] | None = None,
    checkpoint_graph_builder: Callable[[LangfuseRuntime, Any], Any] | None = None,
    checkpoint_database_url: str | None = None,
    checkpoint_factory: Callable[[str], Any] | None = None,
    task_persistence: Any | None = None,
    database_session_factory: ManagedSessionFactory | None = None,
    intent_recognizer: Any | None = None,
    simple_chat_responder: Callable[[str], Any] | None = None,
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
    if checkpoint_graph_builder is not None and any(
        value is not None
        for value in (graph, coordinator, graph_builder)
    ):
        raise ValueError(
            "checkpoint_graph_builder 不能与 graph、coordinator 或 graph_builder 同时提供"
        )
    if checkpoint_graph_builder is None and (
        checkpoint_database_url is not None or checkpoint_factory is not None
    ):
        raise ValueError(
            "checkpoint_database_url 和 checkpoint_factory 需要 checkpoint_graph_builder"
        )

    store = store or TaskStore()
    conversation_store = ConversationRepository(database_session_factory) if database_session_factory is not None else ConversationStore()
    broker = EventBroker()
    langfuse = _create_langfuse_client()
    langfuse_runtime = create_langfuse_runtime(langfuse)
    # 默认应用走图工厂时启用 API 层意图路由；直接注入 graph 的测试/替代
    # 组合可显式注入，避免隐式发起真实 LLM 请求。
    if intent_recognizer is None and (
        graph_builder is not None or checkpoint_graph_builder is not None
    ):
        intent_recognizer = build_intent_recognizer(
            langfuse_runtime.callback_handler
        )
    if simple_chat_responder is None and intent_recognizer is not None:
        simple_chat_responder = _simple_chat_response
    if graph_builder is not None:
        graph = graph_builder(langfuse_runtime)
    task_coordinator = coordinator or TaskCoordinator(
        graph,
        store,
        broker,
        langfuse=langfuse,
        task_persistence=task_persistence,
        intent_recognizer=intent_recognizer,
        simple_chat_responder=simple_chat_responder,
        conversation_store=conversation_store,
    )
    app = FastAPI(
        title="EpiEvidence Research API",
        lifespan=_application_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8443",
            "http://127.0.0.1:8443",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_router(store, task_coordinator, broker, conversation_store))
    app.include_router(create_openai_router(store, task_coordinator, broker))
    app.state.task_store = store
    app.state.event_broker = broker
    app.state.compiled_graph = graph
    app.state.task_coordinator = task_coordinator
    app.state.langfuse = langfuse
    app.state.langfuse_callback_handler = langfuse_runtime.callback_handler
    app.state.langfuse_runtime = langfuse_runtime
    app.state.task_persistence = task_persistence
    app.state.database_session_factory = database_session_factory
    app.state.checkpoint_graph_builder = checkpoint_graph_builder
    app.state.checkpoint_database_url = checkpoint_database_url
    app.state.checkpoint_factory = checkpoint_factory or build_postgres_checkpointer
    app.state.checkpoint_mode = (
        "postgres" if checkpoint_graph_builder is not None else "memory"
    )
    return app


def _create_default_app() -> FastAPI:
    """创建共享 PostgreSQL 生命周期的默认完整研究应用。"""

    session_factory = ManagedSessionFactory()
    return create_app(
        checkpoint_graph_builder=lambda runtime, checkpointer: build_application_graph(
            runtime,
            session_factory=session_factory,
            checkpointer=checkpointer,
        ),
        checkpoint_database_url=os.getenv("CHECKPOINTS_DATABASE_URL"),
        task_persistence=ResearchTaskRepository(session_factory),
        database_session_factory=session_factory,
    )


app = _create_default_app()
