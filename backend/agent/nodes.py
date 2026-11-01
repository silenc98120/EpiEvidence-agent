from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.types import Send
from loguru import logger

from backend.agent.state import EvidenceState
from backend.agent.subgraphs.quality_evaluation import record_article_id, study_group_id
from backend.agent.summarizer import render_summary_chat
from backend.app.cache import RedisWorkCache
from backend.app.core.deduplicator import Deduplicator
from backend.app.core.intent_recognizer import IntentRecognitionResult, IntentRecognizer
from backend.app.core.mesh_normalizer import MeshNormalizationResult, MeshNormalizer
from backend.app.core.quality_candidate_selector import (
    CandidateArticle,
    EmbeddingClient,
    QualityCandidateSelector,
    ResearchProfile,
)
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
from backend.skills.quality_evaluator.schema import StudyType


RequestFactory = Callable[[CompiledSearchQuery, str], Any]
SearchCallable = Callable[[Any], Any | Awaitable[Any]]
RawResultPersister = Callable[[SearchSource, str, Any], Any | Awaitable[Any]]
EmbeddingClientFactory = Callable[[], EmbeddingClient]


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
        existing = state.get("intent_analysis")
        if existing:
            # API 路由层已经识别过研究意图时复用其审计结果，避免重复 LLM 调用。
            result = IntentRecognitionResult.model_validate(existing)
            return {
                "intent_analysis": result.model_dump(mode="json"),
                "task_stage": "intent_recognized",
            }
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


def quality_candidate_selection_node(
    embedding_client: Any,
    *,
    report_directory: Path,
    max_candidates: int = 50,
    embedding_store: Any | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """在质量评价前按关键词、语义和发表时间选择高相关候选。"""

    if max_candidates < 1:
        raise ValueError("max_candidates 必须大于 0")

    async def selection_node(state: EvidenceState) -> dict[str, Any]:
        raw_records = [
            dict(record)
            for record in state.get("search_results", [])
            if isinstance(record, Mapping)
        ]
        profile = ResearchProfile.from_intent(
            state.get("intent_analysis", {}),
            state.get("mesh_normalization", {}),
        )
        records_by_article_id = {
            record_article_id(record): record
            for record in raw_records
        }
        articles = [
            CandidateArticle(
                article_id=article_id,
                title=str(record.get("title", "")).strip(),
                abstract=_optional_text(record.get("abstract")),
                publication_date=_record_publication_date(record),
                publication_year=_optional_int(record.get("publication_year")),
                study_design=_optional_text(record.get("study_design")),
                keywords=tuple(
                    str(item).strip()
                    for item in (
                        record.get("keywords")
                        or record.get("mesh_headings")
                        or record.get("publication_types")
                        or []
                    )
                    if str(item).strip()
                ),
            )
            for article_id, record in records_by_article_id.items()
            if str(record.get("title", "")).strip()
        ]
        owns_client = callable(embedding_client) and not hasattr(embedding_client, "embed")
        client = embedding_client() if owns_client else embedding_client
        embedding_model = str(getattr(client, "model_name", type(client).__name__))
        try:
            scores = await QualityCandidateSelector(
                client,
                embedding_store=embedding_store,
            ).rank(
                profile=profile,
                articles=articles,
            )
        finally:
            close = getattr(client, "close", None)
            if owns_client and close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

        selected_records, report_rows, summary = _select_quality_candidates(
            scores,
            records_by_article_id=records_by_article_id,
            max_candidates=max_candidates,
        )
        report_status, report_path, report_error_type = _write_candidate_ranking_report(
            directory=report_directory,
            task_id=str(state["task_id"]),
            profile=profile,
            embedding_model=embedding_model,
            report_rows=report_rows,
            summary=summary,
        )
        metrics = {
            **state.get("search_metrics", {}),
            "quality_candidate_scored_count": len(scores),
            "quality_candidate_selected_count": len(selected_records),
            "quality_candidate_report_status": report_status,
        }
        return {
            "search_results": selected_records,
            "search_metrics": metrics,
            "quality_candidate_ranking": {
                **summary,
                "report_status": report_status,
                "report_error_type": report_error_type,
            },
            "quality_candidate_report_path": report_path,
            "task_stage": "quality_candidates_selected",
        }

    if langfuse_runtime is None:
        return selection_node
    return langfuse_runtime.wrap_node(
        name="quality-candidate-selection",
        as_type="chain",
        node=selection_node,
        input_metrics=lambda state: {
            "deduplicated_article_count": len(state.get("search_results", [])),
        },
        output_metrics=lambda result: result.get("quality_candidate_ranking", {}),
    )


def _select_quality_candidates(
    scores: list[Any],
    *,
    records_by_article_id: Mapping[str, dict[str, Any]],
    max_candidates: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """应用核心干预、研究设计和配额规则，同时保留完整审计行。"""

    supported_study_types = {study_type.value for study_type in StudyType}
    selected: list[dict[str, Any]] = []
    selected_by_type: dict[str, int] = {}
    report_rows: list[dict[str, Any]] = []
    core_mismatch_count = 0
    unsupported_design_count = 0
    global_limit_count = 0

    for rank, score in enumerate(scores, start=1):
        record = records_by_article_id[score.article.article_id]
        row = score.as_dict(rank)
        study_design = study_group_id(record)
        row["quality_study_design"] = study_design
        selection_status = "selected"
        exclusion_reason: str | None = None
        if not score.core_intervention_matched:
            selection_status = "not_selected"
            exclusion_reason = "core_intervention_not_matched"
            core_mismatch_count += 1
        elif study_design not in supported_study_types:
            selection_status = "not_selected"
            exclusion_reason = "unsupported_study_design"
            unsupported_design_count += 1
        elif len(selected) >= max_candidates:
            selection_status = "not_selected"
            exclusion_reason = "global_candidate_limit_reached"
            global_limit_count += 1
        else:
            selected_by_type[study_design] = selected_by_type.get(study_design, 0) + 1
            selected.append(record)
        row["selection_status"] = selection_status
        row["selection_exclusion_reason"] = exclusion_reason
        report_rows.append(row)

    return selected, report_rows, {
        "scoring_version": "quality-candidate-ranking-v1",
        "weights": {
            "keyword_similarity": 0.50,
            "embedding_similarity": 0.40,
            "recency": 0.10,
        },
        "scored_article_count": len(scores),
        "selected_article_count": len(selected),
        "selected_by_study_type": selected_by_type,
        "core_intervention_not_matched_count": core_mismatch_count,
        "unsupported_study_design_count": unsupported_design_count,
        "global_limit_excluded_count": global_limit_count,
        "max_candidates": max_candidates,
    }


def _write_candidate_ranking_report(
    *,
    directory: Path,
    task_id: str,
    profile: ResearchProfile,
    embedding_model: str,
    report_rows: list[dict[str, Any]],
    summary: Mapping[str, Any],
) -> tuple[str, str, str | None]:
    """写入轻量 JSON 审计产物；文件系统失败不阻塞质量评价。"""

    report_path = directory / f"quality-candidate-ranking-{task_id}.json"
    payload = {
        "task_id": task_id,
        "embedding_model": embedding_model,
        "query_profile": profile.query_text,
        "summary": dict(summary),
        "ranking": report_rows,
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        return "failed", str(report_path), type(exc).__name__
    return "written", str(report_path), None


def _record_publication_date(record: Mapping[str, Any]) -> date | None:
    value = _optional_text(record.get("publication_date"))
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _optional_text(value: Any) -> str | None:
    cleaned = str(value).strip() if value is not None else ""
    return cleaned or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
                quality_evaluation_failures=state.get("quality_evaluation_failures", []),
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
