"""将质量评估候选收敛为可持久化的最终文献推荐。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from backend.app.db import ArticleORM, FullTextResourceORM, TaskORM
from backend.app.prompts.evidence_summarizer_prompt_template import (
    EVIDENCE_SUMMARIZER_SYSTEM_PROMPT,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecommendationOutput(_StrictModel):
    """LLM 对一篇最终推荐唯一需要生成的文字。"""

    article_id: UUID
    abstract_summary: str = Field(min_length=1, max_length=1200)
    recommendation_reason: str = Field(min_length=1, max_length=800)


class SummarizerOutput(_StrictModel):
    """Summarizer 唯一的结构化 LLM 输出契约。"""

    task_id: str = Field(min_length=1)
    selected_article_ids: list[UUID] = Field(default_factory=list, max_length=10)
    recommendations: list[RecommendationOutput] = Field(default_factory=list, max_length=10)
    overall_summary: str = Field(min_length=1, max_length=2400)
    coverage_gaps: list[str] = Field(default_factory=list, max_length=10)
    conflict_summary: str | None = Field(default=None, max_length=1200)
    no_recommendation_reason: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def validate_selection_alignment(self) -> "SummarizerOutput":
        selected = self.selected_article_ids
        recommendation_ids = [item.article_id for item in self.recommendations]
        if len(selected) != len(set(selected)):
            raise ValueError("selected_article_ids 不能重复")
        if len(recommendation_ids) != len(set(recommendation_ids)):
            raise ValueError("recommendations 的 article_id 不能重复")
        if set(selected) != set(recommendation_ids):
            raise ValueError("recommendations 必须完整覆盖 selected_article_ids")
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
    """PostgreSQL 题录读取边界，不属于 LLM 输出。"""

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


def validate_summarizer_input(
    *,
    task_id: str,
    user_query: str,
    ranked_candidates: Sequence[Mapping[str, Any]],
) -> list[UUID]:
    """在调用 LLM 前验证候选外层契约，不重新解释质量事实。"""

    if not str(task_id).strip():
        raise ValueError("task_id 不能为空")
    if not user_query.strip():
        raise ValueError("user_query 不能为空")
    if isinstance(ranked_candidates, (str, bytes)):
        raise ValueError("ranked_candidates 必须是候选对象列表")

    candidate_ids: list[UUID] = []
    seen: set[UUID] = set()
    for index, candidate in enumerate(ranked_candidates):
        if not isinstance(candidate, Mapping):
            raise ValueError(f"ranked_candidates[{index}] 必须是对象")
        try:
            article_id = UUID(str(candidate["article_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"ranked_candidates[{index}] 缺少合法 article_id") from exc
        if article_id in seen:
            raise ValueError("ranked_candidates 的 article_id 不能重复")
        seen.add(article_id)

        try:
            score = float(candidate["corrected_composite_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"ranked_candidates[{article_id}] 缺少可转换的综合分"
            ) from exc
        if not math.isfinite(score):
            raise ValueError(f"ranked_candidates[{article_id}] 综合分必须是有限数值")

        quality_summary = candidate.get("quality_summary")
        if not isinstance(quality_summary, Mapping):
            raise ValueError(f"ranked_candidates[{article_id}] 缺少对象 quality_summary")
        summary_article_id = quality_summary.get("article_id")
        if summary_article_id is not None:
            try:
                summary_uuid = UUID(str(summary_article_id))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"ranked_candidates[{article_id}] quality_summary.article_id 不合法"
                ) from exc
            if summary_uuid != article_id:
                raise ValueError("quality_summary.article_id 必须与候选文章一致")
        candidate_ids.append(article_id)
    return candidate_ids


class SummarizerRepository:
    """按 canonical UUID 读取题录、全文资源并持久化最终 JSONB。"""

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
                article_by_id[article_id], resources_by_article.get(article_id, [])
            )
            for article_id in article_ids
        ]

    async def persist_final_summary(
        self,
        *,
        task_id: str,
        summary: Mapping[str, Any],
    ) -> None:
        """将已合并的最终 JSON 写入任务记录，作为 API 结果的持久化来源。"""

        try:
            canonical_task_id = UUID(task_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("task_id 必须是可持久化的 UUID") from exc

        async with self._session_factory() as session:
            task = await session.get(TaskORM, canonical_task_id)
            if task is None:
                raise LookupError(f"PostgreSQL 未找到 task_id: {task_id}")
            task.final_summary = dict(summary)
            await session.commit()

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
    """调用一次结构化 LLM，并按 article_id 合并不可变事实。"""

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
            SummarizerOutput,
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
    ) -> dict[str, Any]:
        query = user_query.strip()
        candidate_ids = validate_summarizer_input(
            task_id=task_id,
            user_query=query,
            ranked_candidates=ranked_candidates,
        )
        if not candidate_ids:
            summary = self._empty_summary(
                task_id=task_id,
                user_query=query,
                search_metrics=search_metrics,
            )
            await self.repository.persist_final_summary(task_id=task_id, summary=summary)
            return summary

        articles = await self.repository.hydrate(candidate_ids)
        article_by_id = {article.article_id: article for article in articles}
        candidate_by_id = {
            article_id: candidate
            for article_id, candidate in zip(candidate_ids, ranked_candidates, strict=True)
        }
        candidate_payload = [
            self._candidate_payload(
                article=article_by_id[article_id],
                candidate=candidate_by_id[article_id],
                rank=rank,
            )
            for rank, article_id in enumerate(candidate_ids, start=1)
        ]
        output = await self._invoke(
            {
                "task_id": task_id,
                "user_query": query,
                "intent_analysis": dict(intent_analysis),
                "portfolio_evaluation": dict(portfolio_evaluation or {}),
                "ranked_candidates": candidate_payload,
            }
        )
        if output.task_id != task_id:
            raise ValueError("Summarizer 输出 task_id 与输入不一致")
        selected_ids = set(output.selected_article_ids)
        if not selected_ids.issubset(set(candidate_ids)):
            raise ValueError("Summarizer 选择了 ranked_candidates 之外的文章")

        generated_by_id = {item.article_id: item for item in output.recommendations}
        recommendations = [
            self._merge_recommendation(
                article=article_by_id[article_id],
                candidate=candidate_by_id[article_id],
                generated=generated_by_id[article_id],
                rank=rank,
            )
            for rank, article_id in enumerate(candidate_ids, start=1)
            if article_id in selected_ids
        ]
        summary = {
            "task_id": task_id,
            "user_query": query,
            "search_overview": self._search_overview(
                search_metrics,
                retained_count=len(candidate_ids),
                recommended_count=len(recommendations),
            ),
            "overall_summary": output.overall_summary,
            "coverage_gaps": list(output.coverage_gaps),
            "conflict_summary": output.conflict_summary,
            "no_recommendation_reason": output.no_recommendation_reason,
            "recommended_article_ids": [
                recommendation["article_id"] for recommendation in recommendations
            ],
            "recommendations": recommendations,
        }
        await self.repository.persist_final_summary(task_id=task_id, summary=summary)
        return summary

    @staticmethod
    def _candidate_payload(
        *,
        article: SummaryArticle,
        candidate: Mapping[str, Any],
        rank: int,
    ) -> dict[str, Any]:
        article_payload = article.model_dump(mode="json", exclude={"preferred_full_text"})
        article_payload["full_text_available"] = article.preferred_full_text is not None
        article_payload["full_text_format"] = (
            article.preferred_full_text.format if article.preferred_full_text else None
        )
        return {
            **article_payload,
            "rank": rank,
            "corrected_composite_score": float(candidate["corrected_composite_score"]),
            "coverage_contribution_score": float(
                candidate.get("coverage_contribution_score", 0.0)
            ),
            "quality_summary": dict(candidate["quality_summary"]),
            "verification_issues": list(candidate.get("verification_issues", [])),
        }

    @staticmethod
    def _merge_recommendation(
        *,
        article: SummaryArticle,
        candidate: Mapping[str, Any],
        generated: RecommendationOutput,
        rank: int,
    ) -> dict[str, Any]:
        return {
            "article_id": str(article.article_id),
            "rank": rank,
            "title": article.title,
            "authors": article.authors,
            "first_author": article.first_author,
            "journal_title": article.journal_title,
            "publication_date": article.publication_date,
            "publication_year": article.publication_year,
            "study_design": article.study_design,
            "doi": article.doi,
            "pmid": article.pmid,
            "pmcid": article.pmcid,
            "preferred_full_text": (
                article.preferred_full_text.model_dump(mode="json")
                if article.preferred_full_text
                else None
            ),
            "corrected_composite_score": float(candidate["corrected_composite_score"]),
            "coverage_contribution_score": float(
                candidate.get("coverage_contribution_score", 0.0)
            ),
            "quality_summary": dict(candidate["quality_summary"]),
            "verification_issues": list(candidate.get("verification_issues", [])),
            "abstract_summary": generated.abstract_summary,
            "recommendation_reason": generated.recommendation_reason,
        }

    async def _invoke(self, payload: Mapping[str, Any]) -> SummarizerOutput:
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = perf_counter()
            try:
                response = await self._structured_model.ainvoke(messages)
                latency_ms = (perf_counter() - started) * 1000
                if isinstance(response, SummarizerOutput):
                    result = response
                    raw = None
                else:
                    parsing_error = response.get("parsing_error")
                    parsed = response.get("parsed")
                    if parsing_error is not None or parsed is None:
                        raise ValueError("Summarizer 结构化输出校验失败") from parsing_error
                    result = (
                        parsed
                        if isinstance(parsed, SummarizerOutput)
                        else SummarizerOutput.model_validate(parsed)
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
        raise RuntimeError(f"Summarizer 在 {self.max_attempts} 次尝试后仍失败") from last_error

    @staticmethod
    def _empty_summary(
        *,
        task_id: str,
        user_query: str,
        search_metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        reason = "本次没有文献通过摘要相关性、质量核查和最低分筛选。"
        return {
            "task_id": task_id,
            "user_query": user_query,
            "search_overview": EvidenceSummarizer._search_overview(
                search_metrics,
                retained_count=0,
                recommended_count=0,
            ),
            "overall_summary": reason,
            "coverage_gaps": [],
            "conflict_summary": None,
            "no_recommendation_reason": reason,
            "recommended_article_ids": [],
            "recommendations": [],
        }

    @staticmethod
    def _search_overview(
        search_metrics: Mapping[str, Any],
        *,
        retained_count: int,
        recommended_count: int,
    ) -> dict[str, Any]:
        source_statuses = dict(search_metrics.get("source_statuses", {}))
        return {
            "sources": list(source_statuses),
            "source_statuses": source_statuses,
            "provider_record_count": int(search_metrics.get("provider_record_count", 0)),
            "unique_record_count": int(search_metrics.get("unique_record_count", 0)),
            "duplicate_record_count": int(search_metrics.get("duplicate_record_count", 0)),
            "retained_count": retained_count,
            "recommended_count": recommended_count,
        }


def render_summary_chat(summary: Mapping[str, Any]) -> str:
    """将持久化 JSON 渲染成 Chat 页面的一条普通中文消息。"""

    overview = summary.get("search_overview", {})
    if not isinstance(overview, Mapping):
        overview = {}
    recommendations = summary.get("recommendations", [])
    if not isinstance(recommendations, Sequence) or isinstance(recommendations, (str, bytes)):
        recommendations = []
    lines = [
        f"围绕“{summary.get('user_query', '')}”，本次从 {len(overview.get('sources', []))} 个数据源获得 "
        f"{overview.get('provider_record_count', 0)} 条记录，去重后保留 "
        f"{overview.get('unique_record_count', 0)} 篇。",
        "",
        str(summary.get("overall_summary", "")),
    ]
    if not recommendations:
        reason = str(summary.get("no_recommendation_reason") or "目前没有适合推荐的文献。")
        if reason != summary.get("overall_summary"):
            lines.extend(["", f"未推荐文献的原因：{reason}"])
        return "\n".join(lines)

    lines.extend(["", f"最终推荐 {len(recommendations)} 篇文献："])
    for display_index, article in enumerate(recommendations, start=1):
        if not isinstance(article, Mapping):
            continue
        lines.extend(
            [
                "",
                f"{display_index}. {article.get('title', '题名未提供')}",
                _article_metadata_line(article),
                f"摘要概括：{article.get('abstract_summary', '')}",
                f"推荐理由：{article.get('recommendation_reason', '')}",
                f"摘要阶段综合分：{float(article.get('corrected_composite_score', 0.0)):.1f}；"
                f"集合覆盖贡献分：{float(article.get('coverage_contribution_score', 0.0)):.1f}",
            ]
        )
        quality_summary = article.get("quality_summary", {})
        if isinstance(quality_summary, Mapping):
            quality_text = str(quality_summary.get("summary_text", "")).strip()
            if quality_text:
                lines.append(f"质量评估概括：{quality_text}")
        _append_list(lines, "核查提示", article.get("verification_issues", []))
        full_text = article.get("preferred_full_text")
        if isinstance(full_text, Mapping) and full_text.get("url"):
            lines.append(f"全文：[{str(full_text.get('format', 'link')).upper()}]({full_text['url']})")

    _append_list(lines, "整体证据缺口", summary.get("coverage_gaps", []), separated=True)
    if summary.get("conflict_summary"):
        lines.extend(["", f"结果冲突：{summary['conflict_summary']}"])
    lines.extend(
        [
            "",
            "以上评价仅基于题名、摘要和显式元数据，属于初步筛选，不是正式的全文偏倚风险评价。",
        ]
    )
    return "\n".join(lines)


def _article_metadata_line(article: Mapping[str, Any]) -> str:
    author = article.get("first_author") or _first_author_name(article.get("authors", []))
    parts = [str(item) for item in (author, article.get("journal_title")) if item]
    if article.get("publication_year") is not None:
        parts.append(str(article["publication_year"]))
    return "；".join(parts) if parts else "题录信息未完整提供"


def _first_author_name(authors: Any) -> str | None:
    if not isinstance(authors, Sequence) or isinstance(authors, (str, bytes)) or not authors:
        return None
    author = authors[0]
    if not isinstance(author, Mapping):
        return None
    for key in ("full_name", "name", "display_name", "collective_name"):
        value = author.get(key)
        if value:
            return str(value)
    return None


def _append_list(
    lines: list[str],
    label: str,
    items: Any,
    *,
    separated: bool = False,
) -> None:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return
    if separated:
        lines.append("")
    lines.append(f"{label}：" + "；".join(cleaned))
