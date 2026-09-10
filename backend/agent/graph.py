"""EpiEvidence 的 LangGraph 编排节点。

图中只在 State 里传递结构化决策和文章 ID。搜索分支返回统一结果后由 fan-in
节点去重，质量评价分支只接收分组和文章 ID；摘要正文由后续 Repository 从
PostgreSQL 读取，不塞进 Send 消息或 ``messages``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from backend.agent.state import EvidenceState
from backend.agent.summarizer import EvidenceSummarizer, SummarizerRepository
from backend.app.cache import RedisWorkCache
from backend.app.core.deduplicator import Deduplicator
from backend.app.core.intent_recognizer import IntentRecognizer
from backend.app.core.mesh_normalizer import MeshNormalizer, MeshRepository
from backend.app.core.search_results_normalizer import (
    SearchResultsNormalizer,
)
from backend.app.core.search_strategy_generator import SearchPlanBuilder, SearchSource
from backend.app.core.search_source_selector import SearchSourceSelector
from backend.app.db.repositories import SearchResultsRepository
from backend.app.db.session import build_session_factory
from backend.app.observability import LangfuseRuntime

from backend.agent.nodes import (
    SearchBackend,
    default_search_backends as build_search_backends,
    intent_node,
    mesh_normalizer_node,
    search_plan_node,
    search_source_selector_node,
    search_fanout_node,
    search_fanout_marker_node,
    search_source_node,
    search_results_fan_in_node,
    group_articles_node,
    quality_batcher_node,
    quality_group_router,
    quality_evaluator_node,
    quality_evaluation_fan_in_node,
    evidence_summarizer_node,
)
from backend.agent.subgraphs.quality_evaluation import (
    build_quality_evaluation_subgraph,
)

DEFAULT_MESH_DATABASE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "mesh.sqlite3"
)


def _build_default_mesh_normalizer() -> MeshNormalizer:
    """创建完整研究图必需的本地 MeSH 标准化依赖。"""

    return MeshNormalizer(MeshRepository(DEFAULT_MESH_DATABASE_PATH))


def _build_default_search_backends() -> dict[SearchSource, SearchBackend]:
    """注册完整研究图可并行调用的文献检索后端。"""

    from backend.app.tools.article_search.europepmc import EuropePMCSearcher
    from backend.app.tools.article_search.pmc import PMCSearcher
    from backend.app.tools.article_search.pubmed import PubMedSearcher
    from backend.app.tools.article_search.semantic_scholar import (
        SemanticScholarSearcher,
    )

    return build_search_backends(
        pubmed_searcher=PubMedSearcher(),
        europe_pmc_searcher=EuropePMCSearcher(),
        pmc_searcher=PMCSearcher(),
        semantic_scholar_searcher=SemanticScholarSearcher(),
    )


# 应用运行时图始终使用这两项实际依赖；缺少 MeSH SQLite 时导入会立即失败，
# 而不是生成一张会在执行中跳过标准化或无法检索的半成品图。
default_mesh_normalizer = _build_default_mesh_normalizer()
default_search_backends = _build_default_search_backends()

model = ChatOpenAI(
    model=os.getenv("LLM_MODEL","deepseek-v4.1-flash"),
    base_url=os.getenv("LLM_BASE_URL","https://app.deepseek.cn/v1"),
    api_key=os.getenv("LLM_API_KEY"),
    temperature=float(os.getenv("LLM_TEMPERATURE","0.0")),
    timeout=float(os.getenv("LLM_TIMEOUT_SECONDS","120")),
)


def build_intent_recognizer(
    callback_handler: BaseCallbackHandler | None = None,
) -> IntentRecognizer:
    """使用主图共享模型构造意图识别 LLM 节点。"""

    return IntentRecognizer(model=model, callback_handler=callback_handler)


# 无监测配置时，完整图仍可复用同一个模型对象；应用组合层可通过上面的工厂注入
# 当前 Langfuse callback，避免模块导入时隐式创建 SDK 客户端。
intent_recognizer = build_intent_recognizer()


def build_evidence_graph(
    *,
    backends: Mapping[SearchSource, SearchBackend],
    recognizer: IntentRecognizer | None = None,
    mesh_normalizer: MeshNormalizer | None = None,
    search_plan_builder: SearchPlanBuilder | None = None,
    search_source_selector: SearchSourceSelector | None = None,
    quality_evaluator: Any | None = None,
    evidence_summarizer: Any | None = None,
    result_normalizer: SearchResultsNormalizer | None = None,
    deduplicator: Deduplicator | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
    checkpointer: Any | None = None,
    quality_evaluation_subgraph: Any | None = None,
    quality_batch_dispatcher: Any | None = None,
    quality_batch_reader: Any | None = None,
    quality_batch_hydrator: Any | None = None,
    quality_reason_summarizer: Any | None = None,
    quality_max_candidates_before_truncation: int = 50,
    quality_max_candidates_for_summarizer: int = 25,
    quality_reason_summary_batch_size: int = 10,
    quality_poll_interval_seconds: float = 2.0,
    quality_max_poll_interval_seconds: float = 10.0,
    quality_wait_timeout_seconds: float = 900.0,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """构建完整的检索、分组和质量评价图。

    如果没有传入前三个组件，图假设调用方已经在 State 中提供了
    ``intent_analysis``、``mesh_normalization`` 和 ``search_plan``，直接从搜索
    fan-out 开始，便于单独测试后半段。
    """

    if not backends:
        raise ValueError("至少需要注册一个 SearchBackend")
    if any(
        value is not None
        for value in (recognizer, mesh_normalizer, search_plan_builder)
    ) and not all(
        value is not None
        for value in (recognizer, mesh_normalizer, search_plan_builder)
    ):
        raise ValueError(
            "recognizer、mesh_normalizer、search_plan_builder 必须同时提供"
        )

    graph = StateGraph(EvidenceState)
    graph.add_node(
        "search_fanout",
        search_fanout_marker_node(work_cache, langfuse_runtime),
    )

    graph.add_node(
        "search_source",
        search_source_node(
            backends,
            normalizer=result_normalizer,
            work_cache=work_cache,
            persistence=persistence,
            langfuse_runtime=langfuse_runtime,
        ),
    )
    graph.add_node(
        "search_results_fan_in",
        search_results_fan_in_node(
            deduplicator=deduplicator,
            work_cache=work_cache,
            persistence=persistence,
            langfuse_runtime=langfuse_runtime,
        ),
    )
    use_quality_subgraph = quality_evaluator is None
    if use_quality_subgraph:
        compiled_quality_subgraph = quality_evaluation_subgraph
        if compiled_quality_subgraph is None:
            compiled_quality_subgraph = build_quality_evaluation_subgraph(
                work_cache=work_cache,
                checkpointer=None,
                dispatcher=quality_batch_dispatcher,
                reader=quality_batch_reader,
                hydrator=quality_batch_hydrator,
                reason_summarizer=quality_reason_summarizer,
                max_candidates_before_truncation=(
                    quality_max_candidates_before_truncation
                ),
                max_candidates_for_summarizer=quality_max_candidates_for_summarizer,
                reason_summary_batch_size=quality_reason_summary_batch_size,
                poll_interval_seconds=quality_poll_interval_seconds,
                max_poll_interval_seconds=quality_max_poll_interval_seconds,
                wait_timeout_seconds=quality_wait_timeout_seconds,
                parent_success_node="evidence_summarizer",
                parent_timeout_node="evidence_summarizer",
                langfuse_runtime=langfuse_runtime,
            )
        graph.add_node(
            "quality_evaluation",
            compiled_quality_subgraph,
        )
    else:
        graph.add_node(
            "group_articles",
            group_articles_node(work_cache, langfuse_runtime),
        )
        graph.add_node(
            "quality_batcher",
            quality_batcher_node(langfuse_runtime=langfuse_runtime),
        )
        graph.add_node(
            "quality_evaluator",
            quality_evaluator_node(
                quality_evaluator,
                langfuse_runtime=langfuse_runtime,
            ),
        )
        graph.add_node(
            "quality_evaluation_fan_in",
            quality_evaluation_fan_in_node(
                work_cache,
                evaluator=quality_evaluator,
                langfuse_runtime=langfuse_runtime,
            ),
        )
    graph.add_node(
        "evidence_summarizer",
        evidence_summarizer_node(
            evidence_summarizer,
            work_cache,
            langfuse_runtime,
        ),
    )

    if recognizer is not None:
        graph.add_node(
            "intent_recognizer",
            intent_node(recognizer, langfuse_runtime),
        )
        graph.add_node(
            "mesh_normalizer",
            mesh_normalizer_node(mesh_normalizer, langfuse_runtime),
        )
        graph.add_node(
            "search_plan",
            search_plan_node(search_plan_builder, langfuse_runtime),
        )
        graph.add_node(
            "search_source_selector",
            search_source_selector_node(
                search_source_selector or SearchSourceSelector(),
                backends,
                langfuse_runtime,
            ),
        )
        graph.add_edge(START, "intent_recognizer")
        graph.add_edge("intent_recognizer", "mesh_normalizer")
        graph.add_edge("mesh_normalizer", "search_plan")
        graph.add_edge("search_plan", "search_source_selector")
        graph.add_edge("search_source_selector", "search_fanout")
    else:
        graph.add_edge(START, "search_fanout")

    graph.add_conditional_edges(
        "search_fanout",
        search_fanout_node(backends, langfuse_runtime),
    )
    graph.add_edge("search_source", "search_results_fan_in")
    if use_quality_subgraph:
        graph.add_edge("search_results_fan_in", "quality_evaluation")
    else:
        graph.add_edge("search_results_fan_in", "group_articles")
        graph.add_edge("group_articles", "quality_batcher")
        graph.add_conditional_edges(
            "quality_batcher",
            quality_group_router(),
        )
        graph.add_edge("quality_evaluator", "quality_evaluation_fan_in")
        graph.add_edge("quality_evaluation_fan_in", "evidence_summarizer")
    graph.add_edge("evidence_summarizer", END)
    return graph.compile(checkpointer=checkpointer)


def build_runtime_evidence_graph(
    *,
    backends: Mapping[SearchSource, SearchBackend] | None = None,
    mesh_normalizer: MeshNormalizer | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
    callback_handler: BaseCallbackHandler | None = None,
    recognizer: IntentRecognizer | None = None,
    search_plan_builder: SearchPlanBuilder | None = None,
    **graph_dependencies: Any,
):
    """构建包含意图、MeSH 和检索计划节点的完整运行时主图。

    默认使用本模块创建的 MeSH normalizer 和文献搜索后端，确保它始终生成包含
    标准化与并行检索能力的完整应用图。测试或特殊部署可以显式替换它们。
    """

    runtime_callback = (
        callback_handler
        if callback_handler is not None
        else (
            langfuse_runtime.callback_handler
            if langfuse_runtime is not None
            else None
        )
    )
    return build_evidence_graph(
        backends=backends or default_search_backends,
        recognizer=recognizer or (
            build_intent_recognizer(runtime_callback)
            if runtime_callback is not None
            else intent_recognizer
        ),
        mesh_normalizer=mesh_normalizer or default_mesh_normalizer,
        search_plan_builder=search_plan_builder or SearchPlanBuilder(),
        langfuse_runtime=langfuse_runtime,
        **graph_dependencies,
    )


def build_application_graph(
    langfuse_runtime: LangfuseRuntime | None = None,
    *,
    session_factory: Any | None = None,
) -> Any:
    """构造供 FastAPI 注入的完整且可持久化的 compiled LangGraph。"""

    active_session_factory = session_factory or build_session_factory()
    search_results_repository = SearchResultsRepository(active_session_factory)
    evidence_summarizer = EvidenceSummarizer(
        model=model,
        repository=SummarizerRepository(active_session_factory),
    )

    return build_runtime_evidence_graph(
        langfuse_runtime=langfuse_runtime,
        persistence=search_results_repository,
        evidence_summarizer=evidence_summarizer,
    )
