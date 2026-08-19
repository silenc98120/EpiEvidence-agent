"""把质量评价结果整理成最终的简体中文 Chat 消息。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.db.models import ArticleORM, FullTextResourceORM
from app.prompts.evidence_summarizer_prompt_template import (
    EVIDENCE_SUMMARIZER_SYSTEM_PROMPT,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SummaryArticleDraft(_StrictModel):
    """LLM 只负责生成的单篇推荐文字。"""

    article_id: UUID
    abstract_summary: str = Field(min_length=1, max_length=1200)
    recommendation_reason: str = Field(min_length=1, max_length=800)
    strengths: list[str] = Field(default_factory=list, max_length=5)
    limitations: list[str] = Field(default_factory=list, max_length=5)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=5)


class SummarizerDraft(_StrictModel):
    """Summarizer 的内部结构化输出，不直接展示给用户。"""

    task_id: str = Field(min_length=1)
    selected_article_ids: list[UUID] = Field(default_factory=list)
    article_summaries: list[SummaryArticleDraft] = Field(default_factory=list)
    overall_summary: str = Field(min_length=1, max_length=2400)
    coverage_gaps: list[str] = Field(default_factory=list, max_length=10)
    conflict_summary: str | None = Field(default=None, max_length=1200)
    no_recommendation_reason: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def validate_selection_alignment(self) -> "SummarizerDraft":
        selected = self.selected_article_ids
        summary_ids = [item.article_id for item in self.article_summaries]
        if len(selected) != len(set(selected)) or len(summary_ids) != len(set(summary_ids)):
            raise ValueError("推荐文章 UUID 不能重复")
        if set(selected) != set(summary_ids):
            raise ValueError("article_summaries 必须完整覆盖 selected_article_ids")
        if selected and self.no_recommendation_reason is not None:
            raise ValueError("存在推荐文章时不能填写 no_recommendation_reason")
        if not selected and not self.no_recommendation_reason:
            raise ValueError("空推荐集合必须说明 no_recommendation_reason")
        return self


class PreferredFullText(_StrictModel):
    resource_id: UUID
    format: str
    url: str


class SummaryArticle(_StrictModel):
    """从 PostgreSQL 补全、供 Summarizer 分析的候选文章。"""

    article_id: UUID
    title: str
    abstract: str | None = None
    authors: list[dict[str, Any]] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    study_design: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    preferred_full_text: PreferredFullText | None = None


class FinalRecommendedArticle(_StrictModel):
    """程序合并文字草稿、真实题录、分数和全文资源后的推荐项。"""

    article_id: UUID
    rank: int = Field(ge=1)
    title: str
    authors: list[dict[str, Any]] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    study_design: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract_summary: str
    recommendation_reason: str
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    corrected_composite_score: float = Field(ge=0.0, le=10.0)
    coverage_contribution_score: float = Field(ge=0.0, le=10.0)
    verification_issues: list[str] = Field(default_factory=list)
    preferred_full_text: PreferredFullText | None = None


class SearchOverview(_StrictModel):
    sources: list[str] = Field(default_factory=list)
    source_statuses: dict[str, str] = Field(default_factory=dict)
    provider_record_count: int = Field(default=0, ge=0)
    unique_record_count: int = Field(default=0, ge=0)
    duplicate_record_count: int = Field(default=0, ge=0)
    retained_count: int = Field(default=0, ge=0)
    recommended_count: int = Field(default=0, ge=0)


class FinalEvidenceSummary(_StrictModel):
    """后端内部最终契约；前端只显示由它渲染的 Chat 文本。"""

    task_id: str = Field(min_length=1)
    user_query: str = Field(min_length=1)
    search_overview: SearchOverview
    overall_summary: str = Field(min_length=1)
    coverage_gaps: list[str] = Field(default_factory=list)
    conflict_summary: str | None = None
    no_recommendation_reason: str | None = None
    recommendations: list[FinalRecommendedArticle] = Field(default_factory=list)
    recommended_article_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_recommendation_alignment(self) -> "FinalEvidenceSummary":
        ids = [item.article_id for item in self.recommendations]
        if ids != self.recommended_article_ids:
            raise ValueError("recommended_article_ids 必须保持 recommendations 的程序排序")
        if self.search_overview.recommended_count != len(ids):
            raise ValueError("recommended_count 与推荐列表数量不一致")
        return self


class SummarizerRepository:
    """按 canonical UUID 批量读取题录和经过验证的全文资源。"""

    _FORMAT_PRIORITY = {
        "pdf": 0,
        "xml": 1,
        "html": 2,
        "epub": 3,
        "text": 4,
        "other": 5,
    }

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def hydrate(self, article_ids: Sequence[UUID]) -> list[SummaryArticle]:
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("待补全 article_ids 不能重复")
        if not article_ids:
            return []

        article_statement = select(ArticleORM).where(ArticleORM.article_id.in_(article_ids))
        resource_statement = select(FullTextResourceORM).where(
            FullTextResourceORM.article_id.in_(article_ids)
        )
        async with self._session_factory() as session:
            articles = list((await session.scalars(article_statement)).all())
            resources = list((await session.scalars(resource_statement)).all())

        article_by_id = {row.article_id: row for row in articles}
        if len(article_by_id) != len(articles):
            raise ValueError("数据库返回重复 article_id")
        missing = [article_id for article_id in article_ids if article_id not in article_by_id]
        if missing:
            raise LookupError(f"PostgreSQL 未找到 article_id: {missing}")

        resources_by_article: dict[UUID, list[Any]] = {}
        for resource in resources:
            resources_by_article.setdefault(resource.article_id, []).append(resource)

        return [
            self._to_summary_article(
                article_by_id[article_id],
                resources_by_article.get(article_id, []),
            )
            for article_id in article_ids
        ]

    def _to_summary_article(
        self,
        article: Any,
        resources: Sequence[Any],
    ) -> SummaryArticle:
        preferred = self._select_full_text(resources)
        return SummaryArticle(
            article_id=article.article_id,
            title=article.title,
            abstract=article.abstract,
            authors=article.authors or [],
            first_author=article.first_author,
            journal_title=article.journal_title,
            publication_date=(
                article.publication_date.isoformat() if article.publication_date else None
            ),
            publication_year=article.publication_year,
            study_design=article.study_design,
            doi=article.doi,
            pmid=article.pmid,
            pmcid=article.pmcid,
            preferred_full_text=preferred,
        )

    def _select_full_text(self, resources: Sequence[Any]) -> PreferredFullText | None:
        eligible = [
            item
            for item in resources
            if item.is_downloadable is True and item.is_open_access is True
        ]
        if not eligible:
            return None
        selected = min(
            eligible,
            key=lambda item: (
                self._FORMAT_PRIORITY.get(str(item.format).lower(), 99),
                not bool(item.is_preferred),
                str(item.resource_id),
            ),
        )
        return PreferredFullText(
            resource_id=selected.resource_id,
            format=str(selected.format).lower(),
            url=selected.resource_url,
        )


class EvidenceSummarizer:
    """结构化调用 LLM，并确定性生成最终 Chat 回复数据。"""

    def __init__(
        self,
        *,
        model: Any,
        repository: SummarizerRepository,
        system_prompt: str = EVIDENCE_SUMMARIZER_SYSTEM_PROMPT,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self._structured_model = model.with_structured_output(
            SummarizerDraft,
            include_raw=True,
        )
        self.repository = repository
        self.system_prompt = system_prompt
        self.max_attempts = max_attempts

    async def summarize(
        self,
        *,
        task_id: str,
        user_query: str,
        intent_analysis: Mapping[str, Any],
        portfolio_evaluation: Mapping[str, Any] | None,
        ranked_candidates: Sequence[Mapping[str, Any]],
        search_metrics: Mapping[str, Any],
    ) -> FinalEvidenceSummary:
        query = user_query.strip()
        if not query:
            raise ValueError("user_query 不能为空")
        candidate_ids = [UUID(str(item["article_id"])) for item in ranked_candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("ranked_candidates 的 article_id 不能重复")

        if not candidate_ids:
            return self._empty_summary(
                task_id=task_id,
                user_query=query,
                search_metrics=search_metrics,
            )

        articles = await self.repository.hydrate(candidate_ids)
        article_by_id = {article.article_id: article for article in articles}
        candidate_payload = []
        for rank, candidate in enumerate(ranked_candidates, start=1):
            article_id = UUID(str(candidate["article_id"]))
            article = article_by_id[article_id]
            article_payload = article.model_dump(mode="json", exclude={"preferred_full_text"})
            article_payload["full_text_available"] = article.preferred_full_text is not None
            article_payload["full_text_format"] = (
                article.preferred_full_text.format
                if article.preferred_full_text is not None
                else None
            )
            candidate_payload.append(
                {
                    **article_payload,
                    "rank": rank,
                    "corrected_composite_score": float(
                        candidate["corrected_composite_score"]
                    ),
                    "coverage_contribution_score": float(
                        candidate["coverage_contribution_score"]
                    ),
                    "verification_issues": list(
                        candidate.get("verification_issues", [])
                    ),
                }
            )

        draft = await self._invoke(
            {
                "task_id": task_id,
                "user_query": query,
                "intent_analysis": dict(intent_analysis),
                "portfolio_evaluation": dict(portfolio_evaluation or {}),
                "ranked_candidates": candidate_payload,
            }
        )
        if draft.task_id != task_id:
            raise ValueError("Summarizer 输出 task_id 与输入不一致")
        selected_set = set(draft.selected_article_ids)
        if not selected_set.issubset(set(candidate_ids)):
            raise ValueError("Summarizer 选择了 ranked_candidates 之外的文章")

        summaries = {item.article_id: item for item in draft.article_summaries}
        ranked_by_id = {
            UUID(str(item["article_id"])): (rank, item)
            for rank, item in enumerate(ranked_candidates, start=1)
        }
        recommendations = []
        for article_id in candidate_ids:
            if article_id not in selected_set:
                continue
            rank, ranked = ranked_by_id[article_id]
            article = article_by_id[article_id]
            text = summaries[article_id]
            recommendations.append(
                FinalRecommendedArticle(
                    article_id=article_id,
                    rank=rank,
                    title=article.title,
                    authors=article.authors,
                    first_author=article.first_author,
                    journal_title=article.journal_title,
                    publication_date=article.publication_date,
                    publication_year=article.publication_year,
                    study_design=article.study_design,
                    doi=article.doi,
                    pmid=article.pmid,
                    pmcid=article.pmcid,
                    abstract_summary=text.abstract_summary,
                    recommendation_reason=text.recommendation_reason,
                    strengths=text.strengths,
                    limitations=text.limitations,
                    evidence_gaps=text.evidence_gaps,
                    corrected_composite_score=float(
                        ranked["corrected_composite_score"]
                    ),
                    coverage_contribution_score=float(
                        ranked["coverage_contribution_score"]
                    ),
                    verification_issues=list(ranked.get("verification_issues", [])),
                    preferred_full_text=article.preferred_full_text,
                )
            )

        return FinalEvidenceSummary(
            task_id=task_id,
            user_query=query,
            search_overview=self._search_overview(
                search_metrics,
                retained_count=len(candidate_ids),
                recommended_count=len(recommendations),
            ),
            overall_summary=draft.overall_summary,
            coverage_gaps=draft.coverage_gaps,
            conflict_summary=draft.conflict_summary,
            no_recommendation_reason=draft.no_recommendation_reason,
            recommendations=recommendations,
            recommended_article_ids=[item.article_id for item in recommendations],
        )

    async def _invoke(self, payload: Mapping[str, Any]) -> SummarizerDraft:
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        ]
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = perf_counter()
            try:
                response = await self._structured_model.ainvoke(messages)
                latency_ms = (perf_counter() - started) * 1000
                if isinstance(response, SummarizerDraft):
                    result = response
                    raw = None
                else:
                    parsing_error = response.get("parsing_error")
                    parsed = response.get("parsed")
                    if parsing_error is not None or parsed is None:
                        raise ValueError("Summarizer 结构化输出校验失败") from parsing_error
                    result = (
                        parsed
                        if isinstance(parsed, SummarizerDraft)
                        else SummarizerDraft.model_validate(parsed)
                    )
                    raw = response.get("raw")
                usage = getattr(raw, "usage_metadata", None) if raw is not None else None
                if raw is not None and not usage:
                    usage = getattr(raw, "response_metadata", {}).get("token_usage", {})
                logger.bind(
                    component="evidence_summarizer",
                    event="summarization_completed",
                    attempt=attempt,
                    selected_count=len(result.selected_article_ids),
                    latency_ms=round(latency_ms, 1),
                    token_usage=usage or {},
                ).info("最终文献推荐整理完成")
                return result
            except Exception as exc:
                last_error = exc
                logger.bind(
                    component="evidence_summarizer",
                    event="summarization_attempt_failed",
                    attempt=attempt,
                    error_type=type(exc).__name__,
                ).warning("最终文献推荐整理尝试失败")
        raise RuntimeError(
            f"Summarizer 在 {self.max_attempts} 次尝试后仍失败"
        ) from last_error

    def _empty_summary(
        self,
        *,
        task_id: str,
        user_query: str,
        search_metrics: Mapping[str, Any],
    ) -> FinalEvidenceSummary:
        reason = "本次没有文献通过摘要相关性、质量核查和最低分筛选。"
        return FinalEvidenceSummary(
            task_id=task_id,
            user_query=user_query,
            search_overview=self._search_overview(
                search_metrics,
                retained_count=0,
                recommended_count=0,
            ),
            overall_summary=reason,
            no_recommendation_reason=reason,
            recommendations=[],
            recommended_article_ids=[],
        )

    @staticmethod
    def _search_overview(
        search_metrics: Mapping[str, Any],
        *,
        retained_count: int,
        recommended_count: int,
    ) -> SearchOverview:
        source_statuses = dict(search_metrics.get("source_statuses", {}))
        return SearchOverview(
            sources=list(source_statuses),
            source_statuses=source_statuses,
            provider_record_count=int(search_metrics.get("provider_record_count", 0)),
            unique_record_count=int(search_metrics.get("unique_record_count", 0)),
            duplicate_record_count=int(search_metrics.get("duplicate_record_count", 0)),
            retained_count=retained_count,
            recommended_count=recommended_count,
        )


def render_summary_chat(summary: FinalEvidenceSummary) -> str:
    """将内部结构化结果渲染成 Chat 页面的一条普通中文消息。"""

    overview = summary.search_overview
    lines = [
        f"围绕“{summary.user_query}”，本次从 {len(overview.sources)} 个数据源获得 "
        f"{overview.provider_record_count} 条记录，去重后保留 "
        f"{overview.unique_record_count} 篇。",
        "",
        summary.overall_summary,
    ]

    if not summary.recommendations:
        reason = summary.no_recommendation_reason or "目前没有适合推荐的文献。"
        if reason != summary.overall_summary:
            lines.extend(["", f"未推荐文献的原因：{reason}"])
        return "\n".join(lines)

    lines.extend(["", f"最终推荐 {len(summary.recommendations)} 篇文献："])
    for display_index, article in enumerate(summary.recommendations, start=1):
        lines.extend(
            [
                "",
                f"{display_index}. {article.title}",
                _article_metadata_line(article),
                f"摘要概括：{article.abstract_summary}",
                f"推荐理由：{article.recommendation_reason}",
                f"摘要阶段综合分：{article.corrected_composite_score:.1f}；"
                f"集合覆盖贡献分：{article.coverage_contribution_score:.1f}",
            ]
        )
        _append_list(lines, "主要优点", article.strengths)
        _append_list(
            lines,
            "局限与不足",
            [*article.limitations, *article.verification_issues],
        )
        _append_list(lines, "证据缺口", article.evidence_gaps)
        if article.preferred_full_text is not None:
            lines.append(
                f"全文：[{article.preferred_full_text.format.upper()}]"
                f"({article.preferred_full_text.url})"
            )

    _append_list(lines, "整体证据缺口", summary.coverage_gaps, separated=True)
    if summary.conflict_summary:
        lines.extend(["", f"结果冲突：{summary.conflict_summary}"])
    lines.extend(
        [
            "",
            "以上评价仅基于题名、摘要和显式元数据，属于初步筛选，不是正式的全文偏倚风险评价。",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def _article_metadata_line(article: FinalRecommendedArticle) -> str:
    author = article.first_author or _first_author_name(article.authors)
    parts = [item for item in (author, article.journal_title) if item]
    if article.publication_year is not None:
        parts.append(str(article.publication_year))
    return "；".join(parts) if parts else "题录信息未完整提供"


def _first_author_name(authors: Sequence[Mapping[str, Any]]) -> str | None:
    if not authors:
        return None
    author = authors[0]
    for key in ("full_name", "name", "display_name", "collective_name"):
        value = author.get(key)
        if value:
            return str(value)
    return None


def _append_list(
    lines: list[str],
    label: str,
    items: Sequence[str],
    *,
    separated: bool = False,
) -> None:
    cleaned = [item.strip() for item in items if item and item.strip()]
    if not cleaned:
        return
    if separated:
        lines.append("")
    lines.append(f"{label}：" + "；".join(cleaned))
