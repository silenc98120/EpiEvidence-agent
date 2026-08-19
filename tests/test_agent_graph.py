from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from agent.graph import (
    SearchBackend,
    build_evidence_graph,
    create_quality_evaluation_fan_in_node,
    create_quality_evaluator_node,
    _study_group_id,
)
from agent.state import EvidenceState
from app.core.search_results_normalizer import (
    EvidenceRecord,
    UnifiedSearchResult,
    UnifiedSearchStatus,
)
from app.core.search_strategy_generator import (
    CompiledSearchQuery,
    SearchConceptGroup,
    SearchMeshHeading,
    SearchPlan,
    SearchSource,
)
from app.core.intent_recognizer import ResearchKeywordKind


NOW = datetime(2026, 8, 17, tzinfo=UTC)


class RecordingWorkCache:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str]] = []
        self.search_runs: list[tuple[str, list[str]]] = []
        self.groups: list[tuple[str, str, list[str]]] = []

    async def set_task_status(self, task_id: str, status: str, **_metadata: object) -> bool:
        self.statuses.append((task_id, status))
        return True

    async def replace_search_run_article_ids(
        self,
        search_run_id: str,
        article_ids: list[str],
    ) -> bool:
        self.search_runs.append((search_run_id, article_ids))
        return True

    async def replace_group_article_ids(
        self,
        task_id: str,
        group_name: str,
        article_ids: list[str],
    ) -> bool:
        self.groups.append((task_id, group_name, article_ids))
        return True


class FakeCompiler:
    def compile(self, plan: SearchPlan) -> CompiledSearchQuery:
        return CompiledSearchQuery(
            source=SearchSource.PUBMED,
            query=plan.concept_groups[0].raw_text,
            concept_group_count=len(plan.concept_groups),
        )


def _plan() -> SearchPlan:
    return SearchPlan(
        concept_groups=[
            SearchConceptGroup(
                kind=ResearchKeywordKind.DRUG,
                raw_text="司美格鲁肽",
                mesh_headings=[SearchMeshHeading(mesh_id="D000001", label="Semaglutide")],
            )
        ]
    )


def _result(source: SearchSource) -> UnifiedSearchResult:
    record = EvidenceRecord(
        source=source,
        source_record_id=f"{source.value}:record-1",
        source_article_id=f"{source.value}:article-1",
        search_run_id=f"run:{source.value}",
        doi="10.1000/shared",
        title="Semaglutide and weight loss",
        normalized_title="semaglutide and weight loss",
        abstract="Weight loss was measured.",
        abstract_available=True,
        publication_year=2025,
        publication_types=["Randomized Controlled Trial"],
        retrieved_at=NOW,
    )
    return UnifiedSearchResult(
        source=source,
        search_run_id=f"run:{source.value}",
        status=UnifiedSearchStatus.SUCCESS_WITH_RESULTS,
        hit_count=1,
        retrieved_count=1,
        records=[record],
        latency_ms=1.0,
        normalization_latency_ms=0.1,
    )


@pytest.mark.anyio
async def test_graph_fanout_fanin_groups_and_evaluates() -> None:
    calls: list[str] = []
    persisted: list[str] = []
    evaluated_groups: list[dict] = []
    work_cache = RecordingWorkCache()

    async def search_pubmed(_request: object) -> UnifiedSearchResult:
        calls.append("pubmed")
        return _result(SearchSource.PUBMED)

    async def search_europe(_request: object) -> UnifiedSearchResult:
        calls.append("europe_pmc")
        return _result(SearchSource.EUROPE_PMC)

    async def persist_raw(source: SearchSource, run_id: str, _result: object) -> None:
        persisted.append(f"{source.value}:{run_id}")

    async def evaluate(group: dict) -> dict:
        evaluated_groups.append(group)
        return {"status": "screened", "article_count": len(group["article_ids"])}

    backends = {
        SearchSource.PUBMED: SearchBackend(
            source=SearchSource.PUBMED,
            compiler=FakeCompiler(),
            request_factory=lambda compiled, run_id: (compiled.query, run_id),
            search=search_pubmed,
            persist_raw_result=persist_raw,
        ),
        SearchSource.EUROPE_PMC: SearchBackend(
            source=SearchSource.EUROPE_PMC,
            compiler=FakeCompiler(),
            request_factory=lambda compiled, run_id: (compiled.query, run_id),
            search=search_europe,
            persist_raw_result=persist_raw,
        ),
    }
    graph = build_evidence_graph(
        backends=backends,
        quality_evaluator=evaluate,
        work_cache=work_cache,
    )

    output = await graph.ainvoke(
        {
            "task_id": "task-1",
            "task_stage": "created",
            "user_query": "司美格鲁肽的减重效果",
            "query_clarification": False,
            "intent_analysis": {},
            "mesh_normalization": {},
            "search_plan": _plan().model_dump(mode="json"),
            "search_sources": ["pubmed", "europe_pmc"],
            "search_branch_results": [],
            "search_results": [],
            "article_groups": {},
            "quality_groups": [],
            "quality_evaluation_results": [],
            "evaluated_evidence": [],
            "messages": [],
        }
    )

    assert sorted(calls) == ["europe_pmc", "pubmed"]
    assert len(persisted) == 2
    assert len(work_cache.search_runs) == 2
    assert work_cache.statuses[0] == ("task-1", "searching")
    assert ("task-1", "quality_evaluated") in work_cache.statuses
    assert len(work_cache.groups) == 1
    assert len(output["search_results"]) == 1
    assert output["search_metrics"]["duplicate_record_count"] == 1
    grouped_article_ids = output["article_groups"]["randomized_controlled_trial"]
    assert len(grouped_article_ids) == 1
    assert grouped_article_ids[0] in {
        "pubmed:article-1",
        "europe_pmc:article-1",
    }
    assert len(evaluated_groups) == 1
    assert evaluated_groups[0]["article_ids"] == grouped_article_ids
    assert output["evaluated_evidence"][0]["evaluation"] == {
        "status": "screened",
        "article_count": 1,
    }
    assert EvidenceState.__annotations__["messages"] == MessagesState.__annotations__["messages"]


class RecordingQualityPipeline:
    def __init__(self) -> None:
        self.branches: list[dict] = []
        self.finalized_groups: list[dict] = []

    async def evaluate_branch(self, branch: dict) -> dict:
        self.branches.append(branch)
        return {
            "assessment": {"task_id": branch["task_id"]},
            "verification": {"status": "passed"},
            "verified_scores": [],
            "retained_article_ids": [],
            "excluded_article_ids": [],
        }

    async def finalize(self, *, group_results, **_kwargs):
        self.finalized_groups = list(group_results)
        return {
            "portfolio_evaluation": {"recommendation": "covered"},
            "ranked_candidates": [{"article_id": "a"}],
            "retained_candidate_ids": ["a"],
            "excluded_candidate_ids": [],
        }


@pytest.mark.anyio
async def test_quality_nodes_use_pipeline_branch_context_and_finalize_at_fan_in() -> None:
    pipeline = RecordingQualityPipeline()
    evaluate_node = create_quality_evaluator_node(pipeline)
    branch_result = await evaluate_node(
        {
            "task_id": "task-1",
            "user_query": "query",
            "intent_analysis": {"required_concepts": ["query"]},
            "search_plan": {"sources": ["pubmed"]},
            "quality_group": {
                "group_id": "rct-1",
                "study_design": "randomized_controlled_trial",
                "article_ids": ["a"],
            },
        }
    )

    fan_in = create_quality_evaluation_fan_in_node(evaluator=pipeline)
    output = await fan_in(
        {
            "task_id": "task-1",
            "user_query": "query",
            "intent_analysis": {"required_concepts": ["query"]},
            "search_plan": {"sources": ["pubmed"]},
            **branch_result,
        }
    )

    assert pipeline.branches[0]["intent_analysis"] == {
        "required_concepts": ["query"]
    }
    assert pipeline.finalized_groups == [
        branch_result["quality_evaluation_results"][0]["evaluation"]
    ]
    assert output["portfolio_evaluation"] == {"recommendation": "covered"}
    assert output["ranked_candidates"] == [{"article_id": "a"}]


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"study_design": "Randomized Controlled Trial"}, "randomized_controlled_trial"),
        ({"study_design": "Clinical Trial"}, "unknown"),
        ({"study_design": "Cross-sectional prevalence study"}, "cross_sectional_prevalence"),
        ({"study_design": "Analytical cross-sectional study"}, "cross_sectional_analytical"),
        ({"publication_types": ["Meta-Analysis"]}, "meta_analysis"),
        ({"publication_types": ["Systematic Review"]}, "systematic_review"),
        ({"publication_types": ["Umbrella Review"]}, "umbrella_review"),
        ({"publication_types": ["Narrative Review"]}, "narrative_review"),
    ],
)
def test_study_group_routes_to_quality_schema_enum(record: dict, expected: str) -> None:
    assert _study_group_id(record) == expected


@pytest.mark.anyio
async def test_full_graph_places_summarizer_reply_at_end_of_messages() -> None:
    article_id = "a"

    async def search_pubmed(_request: object) -> UnifiedSearchResult:
        return _result(SearchSource.PUBMED)

    class FakePipeline:
        async def evaluate_branch(self, branch):
            return {
                "assessment": {"task_id": branch["task_id"]},
                "verification": {"status": "passed"},
                "verified_scores": [],
                "retained_article_ids": [],
                "excluded_article_ids": [],
            }

        async def finalize(self, **_kwargs):
            return {
                "portfolio_evaluation": {"recommendation": "covered"},
                "ranked_candidates": [
                    {
                        "article_id": article_id,
                        "coverage_contribution_score": 9.0,
                        "corrected_composite_score": 8.0,
                    }
                ],
            }

    class FakeSummarizer:
        async def summarize(self, **_kwargs):
            return {
                "task_id": "task-1",
                "user_query": "query",
                "search_overview": {
                    "recommended_count": 0,
                    "retained_count": 1,
                },
                "overall_summary": "没有需要推荐的文章。",
                "no_recommendation_reason": "候选文章覆盖不足。",
                "recommendations": [],
                "recommended_article_ids": [],
            }

    backends = {
        SearchSource.PUBMED: SearchBackend(
            source=SearchSource.PUBMED,
            compiler=FakeCompiler(),
            request_factory=lambda compiled, run_id: (compiled.query, run_id),
            search=search_pubmed,
        )
    }
    graph = build_evidence_graph(
        backends=backends,
        quality_evaluator=FakePipeline(),
        evidence_summarizer=FakeSummarizer(),
    )
    user_message = HumanMessage(content="query")

    output = await graph.ainvoke(
        {
            "task_id": "task-1",
            "task_stage": "created",
            "user_query": "query",
            "query_clarification": False,
            "intent_analysis": {},
            "mesh_normalization": {},
            "search_plan": _plan().model_dump(mode="json"),
            "search_sources": ["pubmed"],
            "search_branch_results": [],
            "search_results": [],
            "article_groups": {},
            "quality_groups": [],
            "quality_evaluation_results": [],
            "evaluated_evidence": [],
            "messages": [user_message],
        }
    )

    assert output["messages"][0].content == "query"
    assert isinstance(output["messages"][-1], AIMessage)
    assert "没有需要推荐的文章" in output["messages"][-1].content
    assert output["task_stage"] == "completed"


@pytest.mark.anyio
async def test_quality_branch_failure_is_recorded_without_fabricated_scores() -> None:
    class FailingPipeline:
        async def evaluate_branch(self, _branch):
            raise ValueError("canonical UUID required")

    node = create_quality_evaluator_node(FailingPipeline())

    result = await node(
        {
            "task_id": "task-1",
            "quality_group": {
                "group_id": "unknown-1",
                "study_design": "unknown",
                "article_ids": ["source:id"],
            },
        }
    )

    evaluation = result["quality_evaluation_results"][0]["evaluation"]
    assert evaluation == {
        "status": "evaluation_failed",
        "error_type": "ValueError",
        "reason": "canonical UUID required",
    }
