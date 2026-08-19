from __future__ import annotations

from uuid import uuid4

import pytest

from skills.quality_evaluator.schema import (
    ArticleAssessmentBatch,
    ArticleContribution,
    ArticleVerification,
    AnswerabilityVerificationItem,
    ContributionRole,
    CoverageAssessment,
    CoverageStatus,
    CriterionStatus,
    EvaluationArticle,
    EvidenceSource,
    HydratedArticleBatch,
    JointQueryCoverage,
    NarrativeReviewCriterionId,
    NarrativeReviewQualityAssessment,
    PortfolioEvaluation,
    QualityEvaluationJob,
    QualityVerificationItem,
    QueryCoverage,
    RelevanceAssessment,
    RelevanceLevel,
    RelevanceVerificationItem,
    StudyType,
    VerificationBatch,
    VerificationStatus,
)
from skills.quality_evaluator.subgraph import GroupEvaluationResult, QualityEvaluationPipeline


class FakeRepository:
    def __init__(self, batch: HydratedArticleBatch) -> None:
        self.batch = batch
        self.calls = 0

    async def hydrate(self, job, *, user_query):
        self.calls += 1
        assert job == self.batch.job
        assert user_query == self.batch.user_query
        return self.batch


class FakeRuntime:
    def __init__(self, repository, assessment, verification, portfolio) -> None:
        self.repository = repository
        self.assessment = assessment
        self.verification = verification
        self.portfolio = portfolio
        self.portfolio_candidates = None

    async def evaluate(self, job, **_kwargs):
        assert job == self.repository.batch.job
        return self.assessment

    async def verify(self, **kwargs):
        assert kwargs["original"] == self.assessment
        return self.verification

    async def evaluate_portfolio(self, **kwargs):
        self.portfolio_candidates = kwargs["candidate_articles"]
        return self.portfolio


def _fixtures():
    article_id = uuid4()
    job = QualityEvaluationJob(
        task_id="task-1",
        group_id="narrative_review-1",
        study_type=StudyType.NARRATIVE_REVIEW,
        article_ids=[article_id],
    )
    hydrated = HydratedArticleBatch(
        job=job,
        user_query="HPV 疫苗有哪些证据？",
        articles=[
            EvaluationArticle(
                article_id=article_id,
                title="HPV vaccine evidence",
                abstract="This review discusses HPV vaccine benefits and limitations.",
                publication_year=2025,
            )
        ],
    )
    quality = NarrativeReviewQualityAssessment(
        article_id=article_id,
        study_type=StudyType.NARRATIVE_REVIEW,
        criteria=[
            {
                "criterion_id": criterion,
                "status": CriterionStatus.FAVORABLE,
                "score": 1.0,
                "evidence_quote": "This review discusses HPV vaccine benefits and limitations.",
                "reason": "摘要报告了该信息。",
                "evidence_source": EvidenceSource.ABSTRACT,
            }
            for criterion in NarrativeReviewCriterionId
        ],
        strengths=[],
        limitations=[],
        evidence_gaps=[],
        confidence=0.8,
    )
    relevance = RelevanceAssessment(
        article_id=article_id,
        score=9.0,
        level=RelevanceLevel.HIGH,
        reason="直接讨论 HPV 疫苗证据。",
        answerability_score=8.0,
        answerability_reason="摘要同时讨论获益和局限。",
    )
    assessment = ArticleAssessmentBatch(
        task_id=job.task_id,
        group_id=job.group_id,
        study_type=job.study_type,
        article_ids=[article_id],
        relevance_assessments=[relevance],
        quality_assessments=[quality],
    )
    verification = VerificationBatch(
        task_id=job.task_id,
        group_id=job.group_id,
        study_type=job.study_type,
        article_ids=[article_id],
        article_verifications=[
            ArticleVerification(
                article_id=article_id,
                original_total_score=8.7,
                status=VerificationStatus.PASSED,
                relevance=RelevanceVerificationItem(
                    status=VerificationStatus.PASSED,
                    original_level=RelevanceLevel.HIGH,
                    original_score=9.0,
                    corrected_level=RelevanceLevel.HIGH,
                    corrected_score=9.0,
                    evidence_quote_valid=True,
                    reason_supported=True,
                    verification_evidence="HPV vaccine evidence",
                ),
                answerability=AnswerabilityVerificationItem(
                    status=VerificationStatus.PASSED,
                    original_score=8.0,
                    corrected_score=8.0,
                    reason_supported=True,
                    verification_evidence="benefits and limitations",
                ),
                quality_items=[
                    QualityVerificationItem(
                        criterion_id=criterion.value,
                        status=VerificationStatus.PASSED,
                        original_status=CriterionStatus.FAVORABLE,
                        original_score=1.0,
                        corrected_status=CriterionStatus.FAVORABLE,
                        corrected_score=1.0,
                        evidence_quote_valid=True,
                        reason_supported=True,
                        verification_evidence=(
                            "This review discusses HPV vaccine benefits and limitations."
                        ),
                    )
                    for criterion in NarrativeReviewCriterionId
                ],
            )
        ],
    )
    portfolio = PortfolioEvaluation(
        task_id=job.task_id,
        candidate_article_ids=[article_id],
        query_coverage=QueryCoverage(
            required_concepts=["HPV 疫苗"],
            explicit_constraints=[],
            keyword_coverage=[
                CoverageAssessment(
                    concept="HPV 疫苗",
                    status=CoverageStatus.DIRECT,
                    evidence_article_ids=[article_id],
                    reason="直接覆盖。",
                )
            ],
        ),
        joint_query_coverage=JointQueryCoverage(
            status=CoverageStatus.DIRECT,
            evidence_article_ids=[article_id],
            reason="直接覆盖。",
        ),
        coverage_gaps=[],
        redundancy_and_conflicts=[],
        article_contributions=[
            ArticleContribution(
                article_id=article_id,
                role=ContributionRole.CORE_EVIDENCE,
                covered_concepts=["HPV 疫苗"],
                coverage_contribution_score=8.0,
                reason="核心证据。",
            )
        ],
        recommendation="保留。",
    )
    return article_id, job, hydrated, assessment, verification, portfolio


@pytest.mark.anyio
async def test_pipeline_preserves_raw_results_verifies_filters_and_ranks() -> None:
    article_id, job, hydrated, assessment, verification, portfolio = _fixtures()
    repository = FakeRepository(hydrated)
    runtime = FakeRuntime(repository, assessment, verification, portfolio)
    pipeline = QualityEvaluationPipeline(runtime=runtime)

    group = await pipeline.evaluate_branch(
        {
            "task_id": job.task_id,
            "user_query": hydrated.user_query,
            "intent_analysis": {"required_concepts": ["HPV 疫苗"]},
            "search_plan": {},
            "quality_group": {
                "group_id": job.group_id,
                "study_design": job.study_type.value,
                "article_ids": [str(article_id)],
            },
        }
    )
    outcome = await pipeline.finalize(
        task_id=job.task_id,
        user_query=hydrated.user_query,
        intent_analysis={"required_concepts": ["HPV 疫苗"]},
        search_plan={},
        group_results=[group],
    )

    assert group.assessment == assessment
    assert group.verification == verification
    assert group.retained_article_ids == (article_id,)
    assert group.excluded_article_ids == ()
    assert outcome.portfolio == portfolio
    assert [item.article_id for item in outcome.ranked_candidates] == [article_id]
    assert runtime.portfolio_candidates[0]["abstract"] == hydrated.articles[0].abstract
    assert runtime.portfolio_candidates[0]["corrected_composite_score"] == 8.7
    assert repository.calls == 2
    assert GroupEvaluationResult.from_dict(group.to_dict()) == group


@pytest.mark.anyio
async def test_pipeline_rejects_noncanonical_source_ids() -> None:
    _, _, hydrated, assessment, verification, portfolio = _fixtures()
    pipeline = QualityEvaluationPipeline(
        runtime=FakeRuntime(FakeRepository(hydrated), assessment, verification, portfolio)
    )

    with pytest.raises(ValueError, match="canonical UUID"):
        await pipeline.evaluate_branch(
            {
                "task_id": "task-1",
                "user_query": "query",
                "quality_group": {
                    "group_id": "rct-1",
                    "study_design": StudyType.RCT.value,
                    "article_ids": ["pubmed:article-1"],
                },
            }
        )
