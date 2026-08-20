"""Deterministic database selection for one evidence search task."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.intent_recognizer import IntentRecognitionResult, ResearchDirection
from app.core.search_strategy_generator import SearchPlan, SearchSource


class SearchSourceDecision(BaseModel):
    """Structured, user-visible explanation of the selected search sources."""

    selected_sources: list[SearchSource] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)
    source_reasons: dict[SearchSource, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reasons(self) -> "SearchSourceDecision":
        missing = [
            source
            for source in self.selected_sources
            if source not in self.source_reasons
        ]
        if missing:
            raise ValueError(f"缺少检索源选择理由: {missing}")
        return self


class SearchSourceSelector:
    """Select registered databases using deterministic first-version rules.

    PubMed and Europe PMC are the default medical evidence sources. PMC is
    added when the query explicitly asks for full text. Semantic Scholar is
    added for broad or cross-disciplinary questions. Unregistered sources are
    filtered out rather than being sent to the graph router.
    """

    _FULL_TEXT_TERMS = (
        "全文",
        "full text",
        "full-text",
        "open access",
        "开放获取",
        "全文解析",
    )

    def select(
        self,
        *,
        user_query: str,
        intent: IntentRecognitionResult,
        search_plan: SearchPlan,
        available_sources: Iterable[SearchSource],
    ) -> SearchSourceDecision:
        """Return the selected registered sources for the current plan."""

        del search_plan  # Reserved for future plan-specific source policies.
        available = set(available_sources)
        selected: list[SearchSource] = []
        reasons: dict[SearchSource, str] = {}

        self._add_if_available(
            selected,
            reasons,
            SearchSource.PUBMED,
            available,
            "医学题录和临床研究的核心来源",
        )
        self._add_if_available(
            selected,
            reasons,
            SearchSource.EUROPE_PMC,
            available,
            "补充生命科学题录和全文资源",
        )

        normalized_query = user_query.casefold()
        if any(term in normalized_query for term in self._FULL_TEXT_TERMS):
            self._add_if_available(
                selected,
                reasons,
                SearchSource.PMC,
                available,
                "用户明确需要开放全文或全文解析",
            )

        if intent.directions and ResearchDirection.OTHER in intent.directions:
            self._add_if_available(
                selected,
                reasons,
                SearchSource.SEMANTIC_SCHOLAR,
                available,
                "研究方向较宽或跨学科，增加语义检索补充召回",
            )

        if not selected:
            raise ValueError("没有可用的检索后端")

        return SearchSourceDecision(
            selected_sources=selected,
            reason="按医学核心来源、全文需求和研究范围选择检索数据库",
            source_reasons=reasons,
        )

    @staticmethod
    def _add_if_available(
        selected: list[SearchSource],
        reasons: dict[SearchSource, str],
        source: SearchSource,
        available: set[SearchSource],
        reason: str,
    ) -> None:
        if source in available and source not in selected:
            selected.append(source)
            reasons[source] = reason
