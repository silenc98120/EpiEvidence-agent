"""EpiEvidence 的 LangGraph 编排节点。

图中只在 State 里传递结构化决策和文章 ID。搜索分支返回统一结果后由 fan-in
节点去重，质量评价分支只接收分组和文章 ID；摘要正文由后续 Repository 从
PostgreSQL 读取，不塞进 Send 消息或 ``messages``。
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from loguru import logger
from pydantic import BaseModel

from agent.state import EvidenceState
from agent.summarizer import FinalEvidenceSummary, render_summary_chat
from app.cache.redis_work_cache import RedisWorkCache
from app.core.deduplicator import Deduplicator
from app.core.intent_recognizer import IntentRecognitionResult, IntentRecognizer
from app.core.mesh_normalizer import MeshNormalizationResult, MeshNormalizer
from app.core.search_results_normalizer import (
    SearchResultsNormalizer,
    UnifiedSearchError,
    UnifiedSearchResult,
    UnifiedSearchStatus,
)
from app.core.search_strategy_generator import (
    CompiledSearchQuery,
    EuropePMCQueryCompiler,
    PMCQueryCompiler,
    PubMedQueryCompiler,
    SemanticScholarQueryCompiler,
    SearchPlan,
    SearchPlanBuilder,
    SearchSource,
)
from app.core.search_source_selector import SearchSourceSelector
from skills.quality_evaluator.schema import StudyType


RequestFactory = Callable[[CompiledSearchQuery, str], Any]
SearchCallable = Callable[[Any], Any | Awaitable[Any]]
RawResultPersister = Callable[[SearchSource, str, Any], Any | Awaitable[Any]]
QualityEvaluatorCallable = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True)
class SearchBackend:
    """把一个数据库 Compiler、Searcher 和请求模型适配到统一图节点。"""

    source: SearchSource
    compiler: Any
    request_factory: RequestFactory
    search: SearchCallable
    persist_raw_result: RawResultPersister | None = None


def create_default_search_backends(
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

    from app.tools.article_search.europepmc import EuropePMCSearchRequest
    from app.tools.article_search.pmc import PMCSearchRequest
    from app.tools.article_search.pubmed import PubMedSearchRequest
    from app.tools.article_search.semantic_scholar import SemanticScholarSearchRequest

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


def intent_node(recognizer: IntentRecognizer):
    """创建意图识别节点。"""

    async def recognize_intent_node(state: EvidenceState) -> dict[str, Any]:
        result = await recognizer.recognize(state["user_query"])
        return {
            "intent_analysis": result.model_dump(mode="json"),
            "task_stage": "intent_recognized",
        }

    return recognize_intent_node


def create_mesh_normalizer_node(normalizer: MeshNormalizer):
    """创建 MeSH 标准化节点。"""

    def mesh_normalizer_node(state: EvidenceState) -> dict[str, Any]:
        intent = IntentRecognitionResult.model_validate(state["intent_analysis"])
        result = normalizer.normalize(intent)
        return {
            "mesh_normalization": result.model_dump(mode="json"),
            "task_stage": "mesh_normalized",
        }

    return mesh_normalizer_node


def create_search_plan_node(builder: SearchPlanBuilder):
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

    return search_plan_node


def create_search_source_selector_node(
    selector: SearchSourceSelector,
    available_sources: Mapping[SearchSource, SearchBackend],
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

    return search_source_selector_node


def create_search_fanout_node(
    backends: Mapping[SearchSource, SearchBackend],
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

    return route_search_sources


def create_search_fanout_marker_node(work_cache: RedisWorkCache | None = None):
    """创建 fan-out 前的阶段标记节点。"""

    async def search_fanout_node(state: EvidenceState) -> dict[str, Any]:
        if work_cache is not None:
            await work_cache.set_task_status(state["task_id"], "searching")
        return {"task_stage": "searching"}

    return search_fanout_node


def create_search_source_node(
    backends: Mapping[SearchSource, SearchBackend],
    *,
    normalizer: SearchResultsNormalizer | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
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

    return search_source_node


def create_search_results_fan_in_node(
    *,
    deduplicator: Deduplicator | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
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

    return search_results_fan_in_node


def create_group_articles_node(work_cache: RedisWorkCache | None = None):
    """按确定性研究类型规则分组，不让 LLM 自由生成组名。"""

    async def group_articles_node(state: EvidenceState) -> dict[str, Any]:
        groups: dict[str, list[str]] = {}
        group_details: dict[str, dict[str, Any]] = {}
        for record in state.get("search_results", []):
            article_id = _record_article_id(record)
            group_id = _study_group_id(record)
            groups.setdefault(group_id, []).append(article_id)
            group_details.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "study_design": group_id,
                    "article_ids": [],
                },
            )["article_ids"].append(article_id)

        quality_groups = list(group_details.values())
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

    return group_articles_node


def create_quality_group_router():
    """把每个分组派发给独立 Quality Evaluator 分支。"""

    def route_quality_groups(state: EvidenceState) -> list[Send] | str:
        groups = state.get("quality_groups", [])
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


def create_quality_evaluator_node(
    evaluator: QualityEvaluatorCallable | Any | None = None,
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

    return quality_evaluator_node


def create_quality_evaluation_fan_in_node(
    work_cache: RedisWorkCache | None = None,
    *,
    evaluator: QualityEvaluatorCallable | Any | None = None,
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

    return quality_evaluation_fan_in_node


def create_evidence_summarizer_node(
    summarizer: Any | None,
    work_cache: RedisWorkCache | None = None,
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
            summary = (
                result
                if isinstance(result, FinalEvidenceSummary)
                else FinalEvidenceSummary.model_validate(result)
            )
            summary_payload = summary.model_dump(mode="json")
            message = AIMessage(
                content=render_summary_chat(summary),
                additional_kwargs={
                    "task_id": state["task_id"],
                    "recommended_article_ids": [
                        str(article_id)
                        for article_id in summary.recommended_article_ids
                    ],
                },
            )
            if work_cache is not None:
                await work_cache.set_task_status(
                    state["task_id"],
                    "completed",
                    recommended_count=len(summary.recommendations),
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

    return evidence_summarizer_node


def build_evidence_graph(
    *,
    backends: Mapping[SearchSource, SearchBackend],
    recognizer: IntentRecognizer | None = None,
    mesh_normalizer: MeshNormalizer | None = None,
    search_plan_builder: SearchPlanBuilder | None = None,
    search_source_selector: SearchSourceSelector | None = None,
    quality_evaluator: QualityEvaluatorCallable | Any | None = None,
    evidence_summarizer: Any | None = None,
    result_normalizer: SearchResultsNormalizer | None = None,
    deduplicator: Deduplicator | None = None,
    work_cache: RedisWorkCache | None = None,
    persistence: Any | None = None,
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
        create_search_fanout_marker_node(work_cache),
    )
    graph.add_node(
        "search_source",
        create_search_source_node(
            backends,
            normalizer=result_normalizer,
            work_cache=work_cache,
            persistence=persistence,
        ),
    )
    graph.add_node(
        "search_results_fan_in",
        create_search_results_fan_in_node(
            deduplicator=deduplicator,
            work_cache=work_cache,
            persistence=persistence,
        ),
    )
    graph.add_node("group_articles", create_group_articles_node(work_cache))
    graph.add_node(
        "quality_evaluator",
        create_quality_evaluator_node(quality_evaluator),
    )
    graph.add_node(
        "quality_evaluation_fan_in",
        create_quality_evaluation_fan_in_node(
            work_cache,
            evaluator=quality_evaluator,
        ),
    )
    graph.add_node(
        "evidence_summarizer",
        create_evidence_summarizer_node(evidence_summarizer, work_cache),
    )

    if recognizer is not None:
        graph.add_node("intent_recognizer", intent_node(recognizer))
        graph.add_node(
            "mesh_normalizer",
            create_mesh_normalizer_node(mesh_normalizer),
        )
        graph.add_node(
            "search_plan",
            create_search_plan_node(search_plan_builder),
        )
        graph.add_node(
            "search_source_selector",
            create_search_source_selector_node(
                search_source_selector or SearchSourceSelector(),
                backends,
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
        create_search_fanout_node(backends),
    )
    graph.add_edge("search_source", "search_results_fan_in")
    graph.add_edge("search_results_fan_in", "group_articles")
    graph.add_conditional_edges(
        "group_articles",
        create_quality_group_router(),
    )
    graph.add_edge("quality_evaluator", "quality_evaluation_fan_in")
    graph.add_edge("quality_evaluation_fan_in", "evidence_summarizer")
    graph.add_edge("evidence_summarizer", END)
    return graph.compile()


def _record_article_id(record: Mapping[str, Any]) -> str:
    for field_name in ("article_id", "canonical_article_id", "source_article_id"):
        value = record.get(field_name)
        if value:
            return str(value)
    value = record.get("source_record_id")
    if value:
        return str(value)
    raise ValueError("统一文献记录缺少 article_id")


def _study_group_id(record: Mapping[str, Any]) -> str:
    study_design = _clean_group_value(record.get("study_design")) or ""
    publication_types = "_".join(
        _clean_group_value(value) or "" for value in record.get("publication_types", [])
    )
    normalized = "_".join(part for part in (study_design, publication_types) if part)
    if study_design in {study_type.value for study_type in StudyType}:
        return study_design

    aliases = (
        (
            ("qualitative_systematic", "qualitative_evidence_synthesis"),
            StudyType.QUALITATIVE_SYSTEMATIC_REVIEW,
        ),
        (("umbrella_review", "review_of_reviews"), StudyType.UMBRELLA_REVIEW),
        (("meta_analysis", "metaanalysis"), StudyType.META_ANALYSIS),
        (("systematic_review",), StudyType.SYSTEMATIC_REVIEW),
        (("randomized", "randomised"), StudyType.RCT),
        (("case_control",), StudyType.CASE_CONTROL),
        (("cohort",), StudyType.COHORT),
    )
    for needles, study_type in aliases:
        if any(needle in normalized for needle in needles):
            return study_type.value
    if "cross_sectional" in normalized:
        if "prevalence" in normalized:
            return StudyType.CROSS_SECTIONAL_PREVALENCE.value
        return StudyType.CROSS_SECTIONAL_ANALYTICAL.value
    if "narrative_review" in normalized or "review" in normalized:
        return StudyType.NARRATIVE_REVIEW.value
    return "unknown"


def _clean_group_value(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(
        r"[^A-Za-z0-9\u4e00-\u9fff]+",
        "_",
        str(value).strip().casefold(),
    )
    cleaned = cleaned.strip("_")
    return cleaned or None
