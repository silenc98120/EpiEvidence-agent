"""Quality Evaluator 的组级编排与集合级收敛。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from .schema import (
    ArticleAssessmentBatch,
    EvaluationArticle,
    QualityEvaluationJob,
    HydratedArticleBatch,
    StudyType,
)
from .scoring import (
    ArticleScore,
    partition_by_threshold,
    score_assessed_article,
)


MetadataSignalProvider = Callable[[EvaluationArticle], tuple[float, float]]


def neutral_metadata_signals(_article: EvaluationArticle) -> tuple[float, float]:
    """期刊与时效性数据尚不可用时返回中性分，不让 LLM 猜测。"""

    return 5.0, 5.0


@dataclass(frozen=True)
class GroupEvaluationResult:
    job: QualityEvaluationJob
    assessment: ArticleAssessmentBatch
    scores: tuple[ArticleScore, ...]
    retained_article_ids: tuple[UUID, ...]
    excluded_article_ids: tuple[UUID, ...]
    articles: tuple[EvaluationArticle, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "articles": [a.model_dump(mode="json") for a in self.articles],
            "job": self.job.model_dump(mode="json"),
            "assessment": self.assessment.model_dump(mode="json"),
            "scores": [
                {
                    **asdict(score),
                    "article_id": str(score.article_id),
                }
                for score in self.scores
            ],
            "retained_article_ids": [str(item) for item in self.retained_article_ids],
            "excluded_article_ids": [str(item) for item in self.excluded_article_ids],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GroupEvaluationResult":
        return cls(
            articles=tuple(EvaluationArticle.model_validate(a) for a in payload.get("articles", [])),
            job=QualityEvaluationJob.model_validate(payload["job"]),
            assessment=ArticleAssessmentBatch.model_validate(payload["assessment"]),
            scores=tuple(
                ArticleScore(
                    article_id=UUID(str(item["article_id"])),
                    relevance_score=float(item["relevance_score"]),
                    quality_score=float(item["quality_score"]),
                    answerability_score=float(item["answerability_score"]),
                    journal_score=float(item["journal_score"]),
                    recency_score=float(item["recency_score"]),
                    composite_score=float(item["composite_score"]),
                )
                for item in payload["scores"]
            ),
            retained_article_ids=tuple(
                UUID(str(item)) for item in payload["retained_article_ids"]
            ),
            excluded_article_ids=tuple(
                UUID(str(item)) for item in payload["excluded_article_ids"]
            ),
        )


def score_batch(batch: HydratedArticleBatch, assessment: ArticleAssessmentBatch, *,
                metadata_signal_provider: MetadataSignalProvider = neutral_metadata_signals,
                threshold: float = 3.0) -> GroupEvaluationResult:
    """两条执行入口共享同一确定性评分规则，并携带已经读取的文章快照。"""
    relevance = {a.article_id: a for a in assessment.relevance_assessments}
    quality = {a.article_id: a for a in assessment.quality_assessments}
    expected = set(batch.job.article_ids)
    if set(relevance) != expected or set(quality) != expected:
        raise ValueError("评分要求全部文章具有完整评价")
    scores = tuple(score_assessed_article(
        relevance=relevance[a.article_id], quality=quality[a.article_id],
        journal_score=signals[0], recency_score=signals[1])
        for a in batch.articles for signals in [metadata_signal_provider(a)])
    decision = partition_by_threshold(scores, threshold=threshold)
    return GroupEvaluationResult(job=batch.job, assessment=assessment, scores=scores,
        retained_article_ids=tuple(s.article_id for s in decision.retained),
        excluded_article_ids=tuple(s.article_id for s in decision.excluded),
        articles=tuple(batch.articles))
