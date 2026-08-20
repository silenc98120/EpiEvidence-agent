"""Convert internal EvidenceState data into public API result models."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from agent.summarizer import FinalEvidenceSummary
from app.api.schemas import PublicResearchResult


def build_public_result(state: Mapping[str, Any]) -> PublicResearchResult:
    """Validate and expose only the final summary fields needed by the frontend."""

    task_id = UUID(str(state["task_id"]))
    query = str(state.get("user_query", "")).strip()
    if not query:
        raise ValueError("EvidenceState 缺少 user_query")

    raw_summary = state.get("final_summary")
    if not raw_summary:
        raise ValueError("EvidenceState 缺少 final_summary")
    summary = FinalEvidenceSummary.model_validate(raw_summary)
    answer = summary.overall_summary
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
        recommendations=[
            recommendation.model_dump(mode="json")
            for recommendation in summary.recommendations
        ],
        coverage_gaps=summary.coverage_gaps,
        conflict_summary=summary.conflict_summary,
        no_recommendation_reason=summary.no_recommendation_reason,
        search_overview=summary.search_overview.model_dump(mode="json"),
    )
