"""Quality Evaluator 的确定性分数重算、筛选和排序。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isclose
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from .schema import (
    ArticleContribution,
    ArticleVerification,
    RelevanceAssessment,
    VerificationStatus,
)


@dataclass(frozen=True)
class CompositeWeights:
    relevance: float = 0.50
    quality: float = 0.25
    answerability: float = 0.15
    journal: float = 0.05
    recency: float = 0.05

    def __post_init__(self) -> None:
        values = (
            self.relevance,
            self.quality,
            self.answerability,
            self.journal,
            self.recency,
        )
        if any(value < 0 for value in values) or not isclose(sum(values), 1.0):
            raise ValueError("综合分权重必须非负且总和为 1")


@dataclass(frozen=True)
class ArticleScore:
    article_id: UUID
    relevance_score: float
    quality_score: float
    answerability_score: float
    journal_score: float
    recency_score: float
    composite_score: float
    verification_issues: tuple[str, ...]


@dataclass(frozen=True)
class ScreeningDecision:
    retained: tuple[ArticleScore, ...]
    excluded: tuple[ArticleScore, ...]


@dataclass(frozen=True)
class RankedCandidate:
    article_id: UUID
    coverage_contribution_score: float
    corrected_composite_score: float
    publication_date: date | None
    score: ArticleScore
    contribution: ArticleContribution


def calculate_preliminary_quality(scores: Iterable[float | None]) -> float:
    applicable = [score for score in scores if score is not None]
    if not applicable:
        raise ValueError("没有可计分的质量条目")
    if any(score not in {-1.0, -0.5, 1.0} for score in applicable):
        raise ValueError("质量条目分值只能是 +1、-0.5、-1 或 None")
    signal = sum(applicable) / len(applicable)
    return 5.0 + 5.0 * signal


def calculate_composite_score(
    *,
    relevance_score: float,
    quality_score: float,
    answerability_score: float,
    journal_score: float,
    recency_score: float,
    weights: CompositeWeights | None = None,
) -> float:
    components = {
        "relevance_score": relevance_score,
        "quality_score": quality_score,
        "answerability_score": answerability_score,
        "journal_score": journal_score,
        "recency_score": recency_score,
    }
    for name, value in components.items():
        if not 0.0 <= value <= 10.0:
            raise ValueError(f"{name} 必须在 0 到 10 之间")
    selected = weights or CompositeWeights()
    return (
        relevance_score * selected.relevance
        + quality_score * selected.quality
        + answerability_score * selected.answerability
        + journal_score * selected.journal
        + recency_score * selected.recency
    )


def rescore_verified_article(
    *,
    relevance: RelevanceAssessment,
    quality: Any,
    verification: ArticleVerification,
    journal_score: float,
    recency_score: float,
    weights: CompositeWeights | None = None,
) -> ArticleScore:
    article_ids = {relevance.article_id, quality.article_id, verification.article_id}
    if len(article_ids) != 1:
        raise ValueError("相关性、质量和核查结果必须属于同一 article_id")

    quality_score = calculate_preliminary_quality(
        item.corrected_score for item in verification.quality_items
    )
    composite_score = calculate_composite_score(
        relevance_score=verification.relevance.corrected_score,
        quality_score=quality_score,
        answerability_score=verification.answerability.corrected_score,
        journal_score=journal_score,
        recency_score=recency_score,
        weights=weights,
    )
    issues: list[str] = []
    if verification.relevance.status == VerificationStatus.FAILED:
        issues.append(verification.relevance.issue or "相关性评分核查失败")
    if verification.answerability.status == VerificationStatus.FAILED:
        issues.append(verification.answerability.issue or "回答问题能力评分核查失败")
    issues.extend(
        item.issue or f"{item.criterion_id} 核查失败"
        for item in verification.quality_items
        if item.status == VerificationStatus.FAILED
    )
    return ArticleScore(
        article_id=relevance.article_id,
        relevance_score=verification.relevance.corrected_score,
        quality_score=quality_score,
        answerability_score=verification.answerability.corrected_score,
        journal_score=journal_score,
        recency_score=recency_score,
        composite_score=composite_score,
        verification_issues=tuple(issues),
    )


def partition_by_threshold(
    scores: Sequence[ArticleScore],
    *,
    threshold: float = 3.0,
) -> ScreeningDecision:
    retained = tuple(item for item in scores if item.composite_score > threshold)
    excluded = tuple(item for item in scores if item.composite_score <= threshold)
    return ScreeningDecision(retained=retained, excluded=excluded)


def rank_candidates(
    scores: Sequence[ArticleScore],
    contributions: Sequence[ArticleContribution],
    *,
    publication_dates: Mapping[UUID, date | None],
) -> tuple[RankedCandidate, ...]:
    score_by_id = _unique_by_id(scores, "scores")
    contribution_by_id = _unique_by_id(contributions, "contributions")
    expected_ids = set(score_by_id)
    if set(contribution_by_id) != expected_ids:
        raise ValueError("文章分数与集合贡献必须覆盖相同 UUID")
    if set(publication_dates) != expected_ids:
        raise ValueError("publication_dates 必须完整覆盖候选 UUID")

    ranked = [
        RankedCandidate(
            article_id=article_id,
            coverage_contribution_score=contribution_by_id[
                article_id
            ].coverage_contribution_score,
            corrected_composite_score=score.composite_score,
            publication_date=publication_dates[article_id],
            score=score,
            contribution=contribution_by_id[article_id],
        )
        for article_id, score in score_by_id.items()
    ]
    ranked.sort(
        key=lambda item: (
            -item.coverage_contribution_score,
            -item.corrected_composite_score,
            item.publication_date is None,
            -(item.publication_date.toordinal() if item.publication_date else 0),
            str(item.article_id),
        )
    )
    return tuple(ranked)


def _unique_by_id(items: Sequence[Any], name: str) -> dict[UUID, Any]:
    result: dict[UUID, Any] = {}
    for item in items:
        if item.article_id in result:
            raise ValueError(f"{name} 中 article_id 不能重复")
        result[item.article_id] = item
    return result
