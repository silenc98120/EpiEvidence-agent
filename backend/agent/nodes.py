from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.types import Send
from langgraph.graph import END
from loguru import logger
from pydantic import BaseModel

from backend.agent.state import EvidenceState
from backend.agent.subgraphs.quality_evaluation import (
    group_quality_articles,
    record_article_id,
    split_quality_batch,
    study_group_id,
)
from backend.agent.summarizer import render_summary_chat
from backend.app.cache import RedisWorkCache
from backend.app.core.deduplicator import Deduplicator
from backend.app.core.intent_recognizer import IntentRecognitionResult, IntentRecognizer
from backend.app.core.mesh_normalizer import MeshNormalizationResult, MeshNormalizer
from backend.app.core.search_results_normalizer import (
    SearchResultsNormalizer,
    UnifiedSearchError,
    UnifiedSearchResult,
    UnifiedSearchStatus,
)
from backend.app.core.search_strategy_generator import (
    CompiledSearchQuery,
    EuropePMCQueryCompiler,
    PMCQueryCompiler,
    PubMedQueryCompiler,
    SemanticScholarQueryCompiler,
    SearchPlan,
    SearchPlanBuilder,
    SearchSource,
)
from backend.app.core.search_source_selector import SearchSourceSelector
from backend.app.observability import LangfuseRuntime


RequestFactory = Callable[[CompiledSearchQuery, str], Any]
SearchCallable = Callable[[Any], Any | Awaitable[Any]]
RawResultPersister = Callable[[SearchSource, str, Any], Any | Awaitable[Any]]
QualityEvaluatorCallable = Callable[[dict[str, Any]], Any | Awaitable[Any]]


def _mesh_metrics(payload: Mapping[str, Any]) -> dict[str, int]:
    keywords = payload.get("normalized_keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    matched_keyword_count = sum(
        bool(keyword.get("mesh_matches"))
        for keyword in keywords
        if isinstance(keyword, Mapping)
    )
    mesh_match_count = sum(
        len(keyword.get("mesh_matches", []))
        for keyword in keywords
        if isinstance(keyword, Mapping)
    )
    return {
        "normalized_keyword_count": len(keywords),
        "matched_keyword_count": matched_keyword_count,
        "unmatched_keyword_count": len(keywords) - matched_keyword_count,
        "mesh_match_count": mesh_match_count,
    }


def _search_plan_metrics(payload: Mapping[str, Any]) -> dict[str, int]:
    groups = payload.get("concept_groups", [])
    if not isinstance(groups, list):
        groups = []
    return {
        "concept_group_count": len(groups),
        "mesh_heading_count": sum(
            len(group.get("mesh_headings", []))
            for group in groups
            if isinstance(group, Mapping)
        ),
        "free_text_term_count": sum(
            len(group.get("free_text_terms", []))
            for group in groups
            if isinstance(group, Mapping)
        ),
    }


def _search_branch_metrics(result: Mapping[str, Any]) -> dict[str, Any]:
    branch_results = result.get("search_branch_results", [])
    if not isinstance(branch_results, list) or not branch_results:
        return {"status": "missing_search_result"}
    branch = branch_results[0]
    if not isinstance(branch, Mapping):
        return {"status": "invalid_search_result"}
    error = branch.get("error")
    return {
        "source": branch.get("source"),
        "search_run_id": branch.get("search_run_id"),
        "status": branch.get("status"),
        "hit_count": branch.get("hit_count", 0),
        "retrieved_count": branch.get("retrieved_count", 0),
        "latency_ms": branch.get("latency_ms"),
        "normalization_latency_ms": branch.get("normalization_latency_ms"),
        "error_type": error.get("error_type") if isinstance(error, Mapping) else None,
    }


def _quality_branch_metrics(result: Mapping[str, Any]) -> dict[str, Any]:
    evaluations = result.get("quality_evaluation_results", [])
    if not isinstance(evaluations, list) or not evaluations:
        return {"evaluation_status": "missing_evaluation"}
    evaluation = evaluations[0]
    if not isinstance(evaluation, Mapping):
        return {"evaluation_status": "invalid_evaluation"}
    payload = evaluation.get("evaluation", {})
    return {
        "evaluation_status": payload.get("status") if isinstance(payload, Mapping) else None,
        "group_id": evaluation.get("group_id"),
        "article_count": len(evaluation.get("article_ids", [])),
    }


def _summary_answer(result: Mapping[str, Any]) -> str | None:
    messages = result.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return None
    content = getattr(messages[-1], "content", None)
    return content if isinstance(content, str) else None


@dataclass(frozen=True)
class SearchBackend:
    """把一个数据库 Compiler、Searcher 和请求模型适配到统一图节点。"""

    source: SearchSource
    compiler: Any
    request_factory: RequestFactory
    search: SearchCallable
    persist_raw_result: RawResultPersister | None = None


def default_search_backends(
    *,
    pubmed_searcher: Any,
    europe_pmc_searcher: Any,
    pmc_searcher: Any | None = None,
    semantic_scholar_searcher: Any | None = None,
    persist_raw_result: RawResultPersister | None = None,
) -> dict[SearchSource, SearchBackend]:
    """把当前已实现的题录检索器注册为图的 SearchBackend。

    请求对象在每个 Send 分支内创建，因此每个数据源拥有独立的
    ``search_run_id``。bioRxiv/medRxiv 不是这里的普通关键词后端，继续使用
    已有的 Europe PMC DOI 发现与官方接口校验流程。
    """

    from backend.app.tools.article_search.europepmc import EuropePMCSearchRequest
    from backend.app.tools.article_search.pmc import PMCSearchRequest
    from backend.app.tools.article_search.pubmed import PubMedSearchRequest
    from backend.app.tools.article_search.semantic_scholar import SemanticScholarSearchRequest

    backends = {
        SearchSource.PUBMED: SearchBackend(
            source=SearchSource.PUBMED,
            compiler=PubMedQueryCompiler(),
            request_factory=lambda compiled, run_id: PubMedSearchRequest(
                query=compiled.query,
                search_run_id=run_id,
            ),
            search=pubmed_searcher.search,
            persist_raw_result=persist_raw_result,
        ),
        SearchSource.EUROPE_PMC: SearchBackend(
            source=SearchSource.EUROPE_PMC,
            compiler=EuropePMCQueryCompiler(),
            request_factory=lambda compiled, run_id: EuropePMCSearchRequest(
                query=compiled.query,
                search_run_id=run_id,
            ),
            search=europe_pmc_searcher.search,
            persist_raw_result=persist_raw_result,
        ),
    }
    if pmc_searcher is not None:
        backends[SearchSource.PMC] = SearchBackend(
            source=SearchSource.PMC,
            compiler=PMCQueryCompiler(),
            request_factory=lambda compiled, run_id: PMCSearchRequest(
                query=compiled.query,
                search_run_id=run_id,
            ),
            search=pmc_searcher.search,
            persist_raw_result=persist_raw_result,
        )
    if semantic_scholar_searcher is not None:
        backends[SearchSource.SEMANTIC_SCHOLAR] = SearchBackend(
            source=SearchSource.SEMANTIC_SCHOLAR,
            compiler=SemanticScholarQueryCompiler(),
            request_factory=lambda compiled, run_id: SemanticScholarSearchRequest(
                query=compiled.query,
                search_run_id=run_id,
            ),
            search=semantic_scholar_searcher.search,
            persist_raw_result=persist_raw_result,
        )
    return backends


def intent_node(
    recognizer: IntentRecognizer,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建意图识别节点。"""

    async def recognize_intent_node(state: EvidenceState) -> dict[str, Any]:
        result = await recognizer.recognize(state["user_query"])
        return {
            "intent_analysis": result.model_dump(mode="json"),
            "task_stage": "intent_recognized",
        }

    if langfuse_runtime is None:
        return recognize_intent_node
    return langfuse_runtime.wrap_node(
        name="intent-recognizer",
        as_type="agent",
        node=recognize_intent_node,
        input_metrics=lambda state: {"query": state.get("user_query", "")},
        output_metrics=lambda result: {
            "intent_analysis": result.get("intent_analysis", {}),
        },
    )


def mesh_normalizer_node(
    normalizer: MeshNormalizer,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建 MeSH 标准化节点。"""

    def mesh_normalizer_node(state: EvidenceState) -> dict[str, Any]:
        intent = IntentRecognitionResult.model_validate(state["intent_analysis"])
        result = normalizer.normalize(intent)
        return {
            "mesh_normalization": result.model_dump(mode="json"),
            "task_stage": "mesh_normalized",
        }

    if langfuse_runtime is None:
        return mesh_normalizer_node
    return langfuse_runtime.wrap_node(
        name="mesh-normalization",
        as_type="retriever",
        node=mesh_normalizer_node,
        input_metrics=lambda state: {
            "keyword_count": sum(
                len(candidates)
                for candidates in state.get("intent_analysis", {}).get("keywords", {}).values()
            ),
        },
        output_metrics=lambda result: _mesh_metrics(
            result.get("mesh_normalization", {})
        ),
    )


def search_plan_node(
    builder: SearchPlanBuilder,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建数据库无关 SearchPlan 节点。"""

    def search_plan_node(state: EvidenceState) -> dict[str, Any]:
        intent = IntentRecognitionResult.model_validate(state["intent_analysis"])
        mesh_result = MeshNormalizationResult.model_validate(
            state["mesh_normalization"]
        )
        plan = builder.build(intent, mesh_result)
        return {
            "search_plan": plan.model_dump(mode="json"),
            "task_stage": "search_plan_built",
        }

    if langfuse_runtime is None:
        return search_plan_node
    return langfuse_runtime.wrap_node(
        name="search-plan",
        as_type="chain",
        node=search_plan_node,
        input_metrics=lambda state: {
            "direction_count": len(state.get("intent_analysis", {}).get("directions", [])),
            **_mesh_metrics(state.get("mesh_normalization", {})),
        },
        output_metrics=lambda result: _search_plan_metrics(
            result.get("search_plan", {})
        ),
    )


def search_source_selector_node(
    selector: SearchSourceSelector,
    available_sources: Mapping[SearchSource, SearchBackend],
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """Create the Agent node that chooses databases after building SearchPlan."""

    async def search_source_selector_node(state: EvidenceState) -> dict[str, Any]:
        intent = IntentRecognitionResult.model_validate(state["intent_analysis"])
        plan = SearchPlan.model_validate(state["search_plan"])
        decision = selector.select(
            user_query=state.get("user_query", ""),
            intent=intent,
            search_plan=plan,
            available_sources=available_sources.keys(),
        )
        return {
            "search_sources": [source.value for source in decision.selected_sources],
            "search_source_decision": decision.model_dump(mode="json"),
            "task_stage": "search_sources_selected",
        }

    if langfuse_runtime is None:
        return search_source_selector_node
    return langfuse_runtime.wrap_node(
        name="search-source-selection",
        as_type="chain",
        node=search_source_selector_node,
        input_metrics=lambda state: {
            "available_sources": sorted(source.value for source in available_sources),
            **_search_plan_metrics(state.get("search_plan", {})),
        },
        output_metrics=lambda result: {
            "selected_sources": result.get("search_sources", []),
            "selected_source_count": len(result.get("search_sources", [])),
            "decision": result.get("search_source_decision", {}),
        },
    )


def search_fanout_node(
    backends: Mapping[SearchSource, SearchBackend],
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建搜索 fan-out 路由节点。

    每个 ``Send`` 只携带计划、来源和任务标识，不携带摘要列表。
    """

    def route_search_sources(state: EvidenceState) -> list[Send]:
        raw_sources = state.get("search_sources") or [
            source.value for source in backends
        ]
        selected_sources: list[SearchSource] = []
        for raw_source in raw_sources:
            source = SearchSource(raw_source)
            if source not in backends:
                raise ValueError(f"未注册检索后端: {source.value}")
            if source not in selected_sources:
                selected_sources.append(source)

        if not selected_sources:
            raise ValueError("至少需要一个检索数据源")

        plan = SearchPlan.model_validate(state["search_plan"])
        plan_payload = plan.model_dump(mode="json")
        return [
            Send(
                "search_source",
                {
                    "task_id": state["task_id"],
                    "user_query": state.get("user_query", ""),
                    "source": source.value,
                    "search_plan": plan_payload,
                },
            )
            for source in selected_sources
        ]

    if langfuse_runtime is None:
        return route_search_sources
    return langfuse_runtime.wrap_node(
        name="search-fanout",
        as_type="chain",
        node=route_search_sources,
        input_metrics=lambda state: {
            "selected_sources": state.get("search_sources", []),
        },
        output_metrics=lambda sends: {
            "branch_count": len(sends),
            "sources": [
                getattr(send, "arg", {}).get("source")
                for send in sends
            ],
        },
    )


def search_fanout_marker_node(
    work_cache: RedisWorkCache | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建 fan-out 前的阶段标记节点。"""

    async def search_fanout_node(state: EvidenceState) -> dict[str, Any]:
        if work_cache is not None:
            await work_cache.set_task_status(state["task_id"], "searching")
        return {"task_stage": "searching"}

    if langfuse_runtime is None:
        return search_fanout_node
    return langfuse_runtime.wrap_node(
        name="search-dispatch",
        as_type="chain",
        node=search_fanout_node,
        input_metrics=lambda state: {
            "selected_sources": state.get("search_sources", []),
        },
        output_metrics=lambda result: {"task_stage": result.get("task_stage")},
    )


def search_source_node(
    backends: Mapping[SearchSource, SearchBackend],
    *,
    normalizer: SearchResultsNormalizer | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建单个 Send 搜索分支。

    分支内完成请求、可选原始结果持久化和统一化；结果以 JSON 字典返回，供
    ``search_results_fan_in`` 的 reducer 汇总。
    """

    result_normalizer = normalizer or SearchResultsNormalizer()

    async def search_source_node(branch: Mapping[str, Any]) -> dict[str, Any]:
        source = SearchSource(branch["source"])
        backend = backends[source]
        search_run_id = str(uuid4())
        compiled_query = ""
        persistence_failed = False
        try:
            plan = SearchPlan.model_validate(branch["search_plan"])
            compiled = backend.compiler.compile(plan)
            compiled_query = compiled.query
            request = backend.request_factory(compiled, search_run_id)
            try:
                provider_result = backend.search(request)
                if inspect.isawaitable(provider_result):
                    provider_result = await provider_result
            except Exception as exc:
                if persistence is not None:
                    persistence_failed = True
                    try:
                        await persistence.persist_search_failure(
                            task_id=branch["task_id"],
                            user_query=branch.get("user_query", ""),
                            search_run_id=search_run_id,
                            source=source.value,
                            compiled_query=compiled_query,
                            error_type=type(exc).__name__,
                            error_message="搜索源请求失败",
                        )
                    except Exception:
                        raise
                    persistence_failed = False
                raise

            if isinstance(provider_result, UnifiedSearchResult):
                unified = provider_result
            else:
                unified = result_normalizer.normalize_result(provider_result)

            if persistence is not None:
                persistence_failed = True
                try:
                    await persistence.persist_search_result(
                        task_id=branch["task_id"],
                        user_query=branch.get("user_query", ""),
                        search_run_id=search_run_id,
                        source=source.value,
                        compiled_query=compiled_query,
                        provider_result=provider_result,
                        unified_result=unified,
                    )
                except Exception:
                    raise
                persistence_failed = False
            elif backend.persist_raw_result is not None:
                persisted = backend.persist_raw_result(
                    source,
                    search_run_id,
                    provider_result,
                )
                if inspect.isawaitable(persisted):
                    await persisted

            if work_cache is not None:
                await work_cache.replace_search_run_article_ids(
                    unified.search_run_id,
                    [record.source_article_id for record in unified.records],
                )

            return {
                "search_branch_results": [unified.model_dump(mode="json")],
            }
        except Exception as exc:
            if persistence_failed:
                logger.bind(
                    component="search_graph",
                    event="search_persistence_failed",
                    source=source.value,
                    search_run_id=search_run_id,
                    error_type=type(exc).__name__,
                ).exception("搜索结果持久化失败")
                raise
            logger.bind(
                component="search_graph",
                event="search_source_branch_failed",
                source=source.value,
                search_run_id=search_run_id,
                error_type=type(exc).__name__,
            ).exception("搜索分支执行失败")
            failed_result = UnifiedSearchResult(
                source=source,
                search_run_id=search_run_id,
                status=UnifiedSearchStatus.FAILED,
                latency_ms=0.0,
                normalization_latency_ms=0.0,
                error=UnifiedSearchError(
                    error_type=type(exc).__name__,
                    message="搜索分支执行失败",
                    retryable=True,
                ),
            )
            return {
                "search_branch_results": [failed_result.model_dump(mode="json")],
            }

    if langfuse_runtime is None:
        return search_source_node
    return langfuse_runtime.wrap_node(
        name="literature-search",
        as_type="retriever",
        node=search_source_node,
        input_metrics=lambda branch: {
            "source": branch.get("source"),
            "concept_group_count": len(
                branch.get("search_plan", {}).get("concept_groups", [])
            ),
        },
        output_metrics=_search_branch_metrics,
    )


def search_results_fan_in_node(
    *,
    deduplicator: Deduplicator | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建搜索结果 fan-in 节点，统一汇总并去重。"""

    result_deduplicator = deduplicator or Deduplicator()

    async def search_results_fan_in_node(state: EvidenceState) -> dict[str, Any]:
        unified_results = [
            UnifiedSearchResult.model_validate(item)
            for item in state.get("search_branch_results", [])
        ]
        records = [
            record
            for result in unified_results
            for record in result.records
        ]
        deduplicated = result_deduplicator.deduplicate(records)
        persisted_records: list[dict[str, Any]] | None = None
        if persistence is not None:
            persisted_records = await persistence.persist_canonical_results(
                task_id=state["task_id"],
                all_records=records,
                canonical_records=deduplicated.records,
            )
        search_records = persisted_records or [
            record.model_dump(mode="json") for record in deduplicated.records
        ]
        source_statuses = {
            result.source.value: result.status.value for result in unified_results
        }
        if work_cache is not None:
            await work_cache.set_task_status(
                state["task_id"],
                "grouping",
                provider_record_count=len(records),
                unique_record_count=deduplicated.unique_count,
            )
        return {
            "search_results": search_records,
            "search_metrics": {
                "source_statuses": source_statuses,
                "source_count": len(unified_results),
                "provider_record_count": len(records),
                "unique_record_count": deduplicated.unique_count,
                "duplicate_record_count": deduplicated.duplicate_count,
                "deduplication_conflict_count": deduplicated.conflict_count,
            },
            "task_stage": "search_results_merged",
        }

    if langfuse_runtime is None:
        return search_results_fan_in_node
    return langfuse_runtime.wrap_node(
        name="search-results-fan-in",
        as_type="chain",
        node=search_results_fan_in_node,
        input_metrics=lambda state: {
            "source_count": len(state.get("search_branch_results", [])),
        },
        output_metrics=lambda result: result.get("search_metrics", {}),
    )


def group_articles_node(
    work_cache: RedisWorkCache | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """按确定性研究类型规则分组，不让 LLM 自由生成组名。"""

    async def group_articles_node(state: EvidenceState) -> dict[str, Any]:
        groups, quality_groups = group_quality_articles(
            state.get("search_results", [])
        )
        if work_cache is not None:
            for group in quality_groups:
                await work_cache.replace_group_article_ids(
                    state["task_id"],
                    group["group_id"],
                    group["article_ids"],
                )
        return {
            "article_groups": groups,
            "quality_groups": quality_groups,
            "task_stage": "articles_grouped",
        }

    if langfuse_runtime is None:
        return group_articles_node
    return langfuse_runtime.wrap_node(
        name="quality-grouping",
        as_type="chain",
        node=group_articles_node,
        input_metrics=lambda state: {
            "search_result_count": len(state.get("search_results", [])),
        },
        output_metrics=lambda result: {
            "group_count": len(result.get("quality_groups", [])),
            "groups": [
                {
                    "study_design": group.get("study_design"),
                    "article_count": len(group.get("article_ids", [])),
                }
                for group in result.get("quality_groups", [])
            ],
        },
    )
def _split_quality_batch(
    group: Mapping[str, Any],
    *,
    max_batch_size: int = 10,
) -> list[dict[str, Any]]:
    """兼容旧调用方，实际逻辑由质量评估子图提供。"""

    return split_quality_batch(group, max_batch_size=max_batch_size)

def quality_batcher_node(
    *,
    max_batch_size: int = 10,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """将研究类型分组切割成稳定的质量评估批次。"""

    async def quality_batcher_node(
        state: EvidenceState,
    ) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []

        for group in state.get("quality_groups", []):
            batches.extend(
                split_quality_batch(
                    group,
                    max_batch_size=max_batch_size,
                )
            )

        return {
            "quality_batches": batches,
            "task_stage": "quality_batches_created",
        }

    if langfuse_runtime is None:
        return quality_batcher_node
    return langfuse_runtime.wrap_node(
        name="quality-batching",
        as_type="chain",
        node=quality_batcher_node,
        input_metrics=lambda state: {
            "group_count": len(state.get("quality_groups", [])),
            "max_batch_size": max_batch_size,
        },
        output_metrics=lambda result: {
            "batch_count": len(result.get("quality_batches", [])),
            "batch_article_counts": [
                len(batch.get("article_ids", []))
                for batch in result.get("quality_batches", [])
            ],
        },
    )

def quality_group_router():
    """把每个分组派发给独立 Quality Evaluator 分支。"""

    def route_quality_groups(state: EvidenceState) -> list[Send] | str:
        groups = state.get("quality_batches") or state.get("quality_groups", [])
        if not groups:
            return END
        return [
            Send(
                "quality_evaluator",
                {
                    "task_id": state["task_id"],
                    "user_query": state.get("user_query", ""),
                    "intent_analysis": state.get("intent_analysis", {}),
                    "search_plan": state.get("search_plan", {}),
                    "quality_group": group,
                },
            )
            for group in groups
        ]

    return route_quality_groups


def quality_evaluator_node(
    evaluator: QualityEvaluatorCallable | Any | None = None,
    *,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """创建 Quality Evaluator 节点。

    ``evaluator`` 可以是函数，也可以是提供 ``evaluate(group)`` 方法的 skill
    适配器。尚未配置时只返回 ``pending``，不会伪造质量分数。
    """

    async def quality_evaluator_node(branch: Mapping[str, Any]) -> dict[str, Any]:
        group = dict(branch["quality_group"])
        if evaluator is None:
            evaluation: Any = {
                "status": "pending",
                "reason": "quality_evaluator_not_configured",
            }
        else:
            try:
                evaluate_branch = getattr(evaluator, "evaluate_branch", None)
                if evaluate_branch is not None:
                    evaluation = evaluate_branch(branch)
                else:
                    evaluate = getattr(evaluator, "evaluate", evaluator)
                    evaluation = evaluate(group)
                if inspect.isawaitable(evaluation):
                    evaluation = await evaluation
                if hasattr(evaluation, "to_dict"):
                    evaluation = evaluation.to_dict()
                elif isinstance(evaluation, BaseModel):
                    evaluation = evaluation.model_dump(mode="json")
                elif not isinstance(evaluation, dict):
                    evaluation = {"value": evaluation}
            except Exception as exc:
                logger.bind(
                    component="quality_evaluator",
                    event="quality_group_failed",
                    group_id=group["group_id"],
                    error_type=type(exc).__name__,
                ).exception("质量评价分支执行失败")
                evaluation = {
                    "status": "evaluation_failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }

        return {
            "quality_evaluation_results": [
                {
                    "group_id": group["group_id"],
                    "article_ids": group["article_ids"],
                    "evaluation": evaluation,
                }
            ]
        }

    if langfuse_runtime is None:
        return quality_evaluator_node
    return langfuse_runtime.wrap_node(
        name="quality-evaluation-branch",
        as_type="agent",
        node=quality_evaluator_node,
        input_metrics=lambda branch: {
            "group_id": branch.get("quality_group", {}).get("group_id"),
            "study_design": branch.get("quality_group", {}).get("study_design"),
            "article_count": len(
                branch.get("quality_group", {}).get("article_ids", [])
            ),
        },
        output_metrics=_quality_branch_metrics,
    )


def quality_evaluation_fan_in_node(
    work_cache: RedisWorkCache | None = None,
    *,
    evaluator: QualityEvaluatorCallable | Any | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """回收各 Quality Evaluator 分支的结果。"""

    async def quality_evaluation_fan_in_node(state: EvidenceState) -> dict[str, Any]:
        evaluations = list(state.get("quality_evaluation_results", []))
        if work_cache is not None:
            await work_cache.set_task_status(
                state["task_id"],
                "quality_evaluated",
                group_count=len(evaluations),
            )
        result: dict[str, Any] = {
            "evaluated_evidence": evaluations,
            "task_stage": "quality_evaluated",
        }
        finalize = getattr(evaluator, "finalize", None)
        if finalize is None:
            return result

        failures = [
            item
            for item in evaluations
            if item["evaluation"].get("status") == "evaluation_failed"
        ]
        group_results = [
            item["evaluation"]
            for item in evaluations
            if item["evaluation"].get("status") != "evaluation_failed"
        ]
        outcome = finalize(
            task_id=state["task_id"],
            user_query=state.get("user_query", ""),
            intent_analysis=state.get("intent_analysis", {}),
            search_plan=state.get("search_plan", {}),
            group_results=group_results,
        )
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if hasattr(outcome, "to_dict"):
            outcome_payload = outcome.to_dict()
        elif isinstance(outcome, BaseModel):
            outcome_payload = outcome.model_dump(mode="json")
        elif isinstance(outcome, dict):
            outcome_payload = outcome
        else:
            raise TypeError("Quality evaluator finalize 必须返回结构化结果")

        verification_results = [
            group["verification"] for group in group_results if "verification" in group
        ]
        verified_assessments = [
            score
            for group in group_results
            for score in group.get("verified_scores", [])
        ]
        retained_ids = {
            str(article_id)
            for group in group_results
            for article_id in group.get("retained_article_ids", [])
        }
        excluded_ids = {
            str(article_id)
            for group in group_results
            for article_id in group.get("excluded_article_ids", [])
        }
        result.update(
            {
                "verification_results": verification_results,
                "quality_evaluation_failures": failures,
                "verified_assessments": verified_assessments,
                "retained_candidates": [
                    score
                    for score in verified_assessments
                    if str(score["article_id"]) in retained_ids
                ],
                "excluded_candidates": [
                    score
                    for score in verified_assessments
                    if str(score["article_id"]) in excluded_ids
                ],
                "portfolio_evaluation": outcome_payload.get("portfolio_evaluation"),
                "ranked_candidates": outcome_payload.get("ranked_candidates", []),
                "task_stage": "quality_ranked",
            }
        )
        return result

    if langfuse_runtime is None:
        return quality_evaluation_fan_in_node
    return langfuse_runtime.wrap_node(
        name="quality-evaluation-fan-in",
        as_type="chain",
        node=quality_evaluation_fan_in_node,
        input_metrics=lambda state: {
            "evaluation_group_count": len(state.get("quality_evaluation_results", [])),
        },
        output_metrics=lambda result: {
            "retained_candidate_count": len(result.get("retained_candidates", [])),
            "excluded_candidate_count": len(result.get("excluded_candidates", [])),
            "failure_count": len(result.get("quality_evaluation_failures", [])),
            "ranked_candidate_count": len(result.get("ranked_candidates", [])),
        },
    )


def evidence_summarizer_node(
    summarizer: Any | None,
    work_cache: RedisWorkCache | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """把排序后的候选整理成最后一条助手 Chat 消息。"""

    async def evidence_summarizer_node(state: EvidenceState) -> dict[str, Any]:
        if summarizer is None:
            logger.bind(
                component="evidence_summarizer",
                event="summarizer_not_configured",
                task_id=state["task_id"],
            ).warning("未配置 EvidenceSummarizer，跳过最终聊天回复")
            return {"task_stage": state.get("task_stage", "quality_ranked")}

        summarize = getattr(summarizer, "summarize", summarizer)
        try:
            result = summarize(
                task_id=state["task_id"],
                user_query=state.get("user_query", ""),
                intent_analysis=state.get("intent_analysis", {}),
                portfolio_evaluation=state.get("portfolio_evaluation"),
                ranked_candidates=state.get("ranked_candidates", []),
                search_metrics=state.get("search_metrics", {}),
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, Mapping):
                raise ValueError("EvidenceSummarizer 必须返回最终 JSON 对象")
            summary_payload = dict(result)
            recommendations = summary_payload.get("recommendations", [])
            if not isinstance(recommendations, list):
                raise ValueError("最终 JSON 字段 recommendations 必须是列表")
            recommended_article_ids = summary_payload.get("recommended_article_ids", [])
            if not isinstance(recommended_article_ids, list):
                raise ValueError("最终 JSON 字段 recommended_article_ids 必须是列表")
            message = AIMessage(
                content=render_summary_chat(summary_payload),
                additional_kwargs={
                    "task_id": state["task_id"],
                    "recommended_article_ids": [str(article_id) for article_id in recommended_article_ids],
                },
            )
            if work_cache is not None:
                await work_cache.set_task_status(
                    state["task_id"],
                    "completed",
                    recommended_count=len(recommendations),
                )
            return {
                "final_summary": summary_payload,
                "messages": [message],
                "task_stage": "completed",
            }
        except Exception as exc:
            retry_count = int(state.get("summary_retry_count", 0)) + 1
            logger.bind(
                component="evidence_summarizer",
                event="summarization_failed",
                task_id=state["task_id"],
                retry_count=retry_count,
                error_type=type(exc).__name__,
            ).exception("最终聊天回复生成失败")
            if work_cache is not None:
                await work_cache.set_task_status(
                    state["task_id"],
                    "summary_failed",
                    retry_count=retry_count,
                )
            failure = {
                "status": "summary_failed",
                "error_type": type(exc).__name__,
                "retryable": True,
            }
            return {
                "final_summary": failure,
                "summary_retry_count": retry_count,
                "messages": [
                    AIMessage(
                        content=(
                            "文献检索和质量评价已经完成，但最终推荐内容暂时生成失败。"
                            "你可以稍后重试本次总结。"
                        ),
                        additional_kwargs={
                            "task_id": state["task_id"],
                            "summary_error": failure,
                        },
                    )
                ],
                "task_stage": "summary_failed",
            }

    if langfuse_runtime is None:
        return evidence_summarizer_node
    return langfuse_runtime.wrap_node(
        name="evidence-summary",
        as_type="chain",
        node=evidence_summarizer_node,
        input_metrics=lambda state: {
            "ranked_candidate_count": len(state.get("ranked_candidates", [])),
            "search_metrics": state.get("search_metrics", {}),
        },
        output_metrics=lambda result: {
            "summary_status": result.get("task_stage"),
            "answer": _summary_answer(result.get("final_summary")),
            "recommendation_count": len(
                result.get("final_summary", {}).get("recommendations", [])
                if isinstance(result.get("final_summary"), Mapping)
                else []
            ),
            "coverage_gap_count": len(
                result.get("final_summary", {}).get("coverage_gaps", [])
                if isinstance(result.get("final_summary"), Mapping)
                else []
            ),
        },
    )


_record_article_id = record_article_id
_study_group_id = study_group_id
