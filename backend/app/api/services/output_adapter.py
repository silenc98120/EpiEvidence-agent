"""Convert internal EvidenceState data into public API result models."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from backend.app.api.schemas import PublicResearchResult


def build_public_result(state: Mapping[str, Any]) -> PublicResearchResult:
    """Validate and expose only the final summary fields needed by the frontend."""

    task_id = UUID(str(state["task_id"]))
    query = str(state.get("user_query", "")).strip()
    if not query:
        raise ValueError("EvidenceState 缺少 user_query")

    raw_summary = state.get("final_summary")
    if not raw_summary:
        raise ValueError("EvidenceState 缺少 final_summary")
    if not isinstance(raw_summary, Mapping):
        raise ValueError("EvidenceState 的 final_summary 必须是对象")
    overall_summary = raw_summary.get("overall_summary")
    if not isinstance(overall_summary, str) or not overall_summary.strip():
        raise ValueError("final_summary 缺少非空 overall_summary")
    recommendations = raw_summary.get("recommendations", [])
    if not isinstance(recommendations, list) or not all(
        isinstance(item, Mapping) for item in recommendations
    ):
        raise ValueError("final_summary.recommendations 必须是对象列表")
    coverage_gaps = raw_summary.get("coverage_gaps", [])
    if not isinstance(coverage_gaps, list) or not all(
        isinstance(item, str) for item in coverage_gaps
    ):
        raise ValueError("final_summary.coverage_gaps 必须是字符串列表")
    search_overview = raw_summary.get("search_overview", {})
    if not isinstance(search_overview, Mapping):
        raise ValueError("final_summary.search_overview 必须是对象")
    conflict_summary = raw_summary.get("conflict_summary")
    if conflict_summary is not None and not isinstance(conflict_summary, str):
        raise ValueError("final_summary.conflict_summary 必须是字符串或 null")
    no_recommendation_reason = raw_summary.get("no_recommendation_reason")
    if no_recommendation_reason is not None and not isinstance(
        no_recommendation_reason, str
    ):
        raise ValueError("final_summary.no_recommendation_reason 必须是字符串或 null")

    answer = overall_summary.strip()
    messages = state.get("messages", [])
    if messages:
        last_message = messages[-1]
        content = getattr(last_message, "content", None)
        if isinstance(content, str) and content.strip():
            answer = content.strip()

    return PublicResearchResult(
        task_id=task_id,
        query=query,
        answer=answer,
        recommendations=[dict(recommendation) for recommendation in recommendations],
        coverage_gaps=coverage_gaps,
        conflict_summary=conflict_summary,
        no_recommendation_reason=no_recommendation_reason,
        search_overview=dict(search_overview),
    )
