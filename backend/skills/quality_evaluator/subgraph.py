"""Quality Evaluator 的组级编排与集合级收敛。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from .schema import (
    ArticleAssessmentBatch,
    EvaluationArticle,
    PortfolioEvaluation,
    QualityEvaluationJob,
    StudyType,
    VerificationBatch,
)
from .scoring import (
    ArticleScore,
    RankedCandidate,
    calculate_composite_score,
    calculate_preliminary_quality,
    partition_by_threshold,
    rank_candidates,
    rescore_verified_article,
)


MetadataSignalProvider = Callable[[EvaluationArticle], tuple[float, float]]


def neutral_metadata_signals(_article: EvaluationArticle) -> tuple[float, float]:
    """期刊与时效性数据尚不可用时返回中性分，不让 LLM 猜测。"""

    return 5.0, 5.0


@dataclass(frozen=True)
class GroupEvaluationResult:
    job: QualityEvaluationJob
    assessment: ArticleAssessmentBatch
    verification: VerificationBatch
    verified_scores: tuple[ArticleScore, ...]
    retained_article_ids: tuple[UUID, ...]
    excluded_article_ids: tuple[UUID, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.model_dump(mode="json"),
            "assessment": self.assessment.model_dump(mode="json"),
            "verification": self.verification.model_dump(mode="json"),
            "verified_scores": [
                {
                    **asdict(score),
                    "article_id": str(score.article_id),
                    "verification_issues": list(score.verification_issues),
                }
                for score in self.verified_scores
            ],
            "retained_article_ids": [str(item) for item in self.retained_article_ids],
            "excluded_article_ids": [str(item) for item in self.excluded_article_ids],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GroupEvaluationResult":
        return cls(
            job=QualityEvaluationJob.model_validate(payload["job"]),
            assessment=ArticleAssessmentBatch.model_validate(payload["assessment"]),
            verification=VerificationBatch.model_validate(payload["verification"]),
            verified_scores=tuple(
                ArticleScore(
                    article_id=UUID(str(item["article_id"])),
                    relevance_score=float(item["relevance_score"]),
                    quality_score=float(item["quality_score"]),
                    answerability_score=float(item["answerability_score"]),
                    journal_score=float(item["journal_score"]),
                    recency_score=float(item["recency_score"]),
                    composite_score=float(item["composite_score"]),
                    verification_issues=tuple(item.get("verification_issues", [])),
                )
                for item in payload["verified_scores"]
            ),
            retained_article_ids=tuple(
                UUID(str(item)) for item in payload["retained_article_ids"]
            ),
            excluded_article_ids=tuple(
                UUID(str(item)) for item in payload["excluded_article_ids"]
            ),
        )


@dataclass(frozen=True)
class QualityEvaluationOutcome:
    group_results: tuple[GroupEvaluationResult, ...]
    portfolio: PortfolioEvaluation | None
    ranked_candidates: tuple[RankedCandidate, ...]

    @property
    def retained_article_ids(self) -> tuple[UUID, ...]:
        return tuple(item.article_id for item in self.ranked_candidates)

    @property
    def excluded_article_ids(self) -> tuple[UUID, ...]:
        return tuple(
            article_id
            for group in self.group_results
            for article_id in group.excluded_article_ids
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_evaluation": (
                self.portfolio.model_dump(mode="json") if self.portfolio else None
            ),
            "ranked_candidates": [
                {
                    "article_id": str(item.article_id),
                    "coverage_contribution_score": item.coverage_contribution_score,
                    "corrected_composite_score": item.corrected_composite_score,
                    "publication_date": (
                        item.publication_date.isoformat() if item.publication_date else None
                    ),
                    "verification_issues": list(item.score.verification_issues),
                }
                for item in self.ranked_candidates
            ],
            "retained_candidate_ids": [str(item) for item in self.retained_article_ids],
            "excluded_candidate_ids": [str(item) for item in self.excluded_article_ids],
        }


class QualityEvaluationPipeline:
    """顺序执行初筛、核查、重算，并在 fan-in 后执行集合评价。"""

    def __init__(
        self,
        *,
        runtime: Any,
        metadata_signal_provider: MetadataSignalProvider | None = None,
        threshold: float = 3.0,
    ) -> None:
        self.runtime = runtime
        self.metadata_signal_provider = (
            metadata_signal_provider or neutral_metadata_signals
        )
        self.threshold = threshold

    async def evaluate_branch(self, branch: Mapping[str, Any]) -> GroupEvaluationResult:
        group = dict(branch["quality_group"])
        raw_ids = group.get("article_ids", [])
        try:
            article_ids = [UUID(str(article_id)) for article_id in raw_ids]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Quality Evaluator 只接受 PostgreSQL articles.article_id canonical UUID"
            ) from exc
        try:
            study_type = StudyType(group["study_design"])
        except (KeyError, ValueError) as exc:
            raise ValueError("quality_group.study_design 不是受支持的 StudyType") from exc
        job = QualityEvaluationJob(
            task_id=str(branch["task_id"]),
            group_id=str(group["group_id"]),
            study_type=study_type,
            article_ids=article_ids,
        )
        user_query = str(branch.get("user_query", "")).strip()
        assessment = await self.runtime.evaluate(
            job,
            user_query=user_query,
            intent_analysis=branch.get("intent_analysis", {}),
            search_plan=branch.get("search_plan", {}),
        )
        hydrated = await self.runtime.repository.hydrate(job, user_query=user_query)
        article_by_id = {article.article_id: article for article in hydrated.articles}
        relevance_by_id = {
            item.article_id: item for item in assessment.relevance_assessments
        }
        quality_by_id = {
            item.article_id: item for item in assessment.quality_assessments
        }
        original_total_scores: dict[UUID, float] = {}
        metadata_scores: dict[UUID, tuple[float, float]] = {}
        for article_id in job.article_ids:
            journal_score, recency_score = self.metadata_signal_provider(
                article_by_id[article_id]
            )
            metadata_scores[article_id] = (journal_score, recency_score)
            quality_score = calculate_preliminary_quality(
                item.score for item in quality_by_id[article_id].criteria
            )
            relevance = relevance_by_id[article_id]
            original_total_scores[article_id] = calculate_composite_score(
                relevance_score=relevance.score,
                quality_score=quality_score,
                answerability_score=relevance.answerability_score,
                journal_score=journal_score,
                recency_score=recency_score,
            )

        verification = await self.runtime.verify(
            hydrated=hydrated,
            original=assessment,
            original_total_scores=original_total_scores,
        )
        verification_by_id = {
            item.article_id: item for item in verification.article_verifications
        }
        verified_scores = tuple(
            rescore_verified_article(
                relevance=relevance_by_id[article_id],
                quality=quality_by_id[article_id],
                verification=verification_by_id[article_id],
                journal_score=metadata_scores[article_id][0],
                recency_score=metadata_scores[article_id][1],
            )
            for article_id in job.article_ids
        )
        decision = partition_by_threshold(verified_scores, threshold=self.threshold)
        return GroupEvaluationResult(
            job=job,
            assessment=assessment,
            verification=verification,
            verified_scores=verified_scores,
            retained_article_ids=tuple(item.article_id for item in decision.retained),
            excluded_article_ids=tuple(item.article_id for item in decision.excluded),
        )

    async def finalize(
        self,
        *,
        task_id: str,
        user_query: str,
        intent_analysis: Mapping[str, Any],
        search_plan: Mapping[str, Any],
        group_results: Sequence[GroupEvaluationResult | Mapping[str, Any]],
    ) -> QualityEvaluationOutcome:
        groups = tuple(
            item
            if isinstance(item, GroupEvaluationResult)
            else GroupEvaluationResult.from_dict(item)
            for item in group_results
        )
        if any(group.job.task_id != task_id for group in groups):
            raise ValueError("group_results 包含其他 task_id")
        retained_scores = [
            score
            for group in groups
            for score in group.verified_scores
            if score.article_id in set(group.retained_article_ids)
        ]
        if not retained_scores:
            return QualityEvaluationOutcome(
                group_results=groups,
                portfolio=None,
                ranked_candidates=(),
            )

        retained_ids = {score.article_id for score in retained_scores}
        candidate_articles: list[dict[str, Any]] = []
        publication_dates: dict[UUID, Any] = {}
        score_by_id = {score.article_id: score for score in retained_scores}
        for group in groups:
            group_retained = retained_ids.intersection(group.job.article_ids)
            if not group_retained:
                continue
            hydrated = await self.runtime.repository.hydrate(
                group.job,
                user_query=user_query,
            )
            for article in hydrated.articles:
                if article.article_id not in group_retained:
                    continue
                score = score_by_id[article.article_id]
                candidate_articles.append(
                    {
                        **article.model_dump(mode="json"),
                        "study_type": group.job.study_type.value,
                        "corrected_composite_score": score.composite_score,
                        "verification_issues": list(score.verification_issues),
                    }
                )
                publication_dates[article.article_id] = article.publication_date

        portfolio = await self.runtime.evaluate_portfolio(
            task_id=task_id,
            user_query=user_query,
            intent_analysis=intent_analysis,
            search_plan=search_plan,
            candidate_articles=candidate_articles,
        )
        ranked = rank_candidates(
            retained_scores,
            portfolio.article_contributions,
            publication_dates=publication_dates,
        )
        return QualityEvaluationOutcome(
            group_results=groups,
            portfolio=portfolio,
            ranked_candidates=ranked,
        )
