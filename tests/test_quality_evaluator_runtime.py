from __future__ import annotations

import json
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from skills.quality_evaluator.evaluator import QualityEvaluatorRuntime
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
    PortfolioEvaluation,
    QualityEvaluationJob,
    QualityVerificationItem,
    QueryCoverage,
    RCTCriterionId,
    RCTQualityAssessment,
    RelevanceAssessment,
    RelevanceLevel,
    RelevanceVerificationItem,
    StudyType,
    VerificationBatch,
    VerificationStatus,
    article_assessment_model_for,
)


class FakeRunnable:
    def __init__(self, owner: "FakeStructuredModel") -> None:
        self.owner = owner

    async def ainvoke(self, messages):
        self.owner.message_calls.append(messages)
        response = self.owner.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeStructuredModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.schemas: list[type] = []
        self.message_calls: list[list[object]] = []

    def with_structured_output(self, schema):
        self.schemas.append(schema)
        return FakeRunnable(self)


class FakeRepository:
    def __init__(self, batch: HydratedArticleBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[QualityEvaluationJob, str]] = []

    async def hydrate(self, job, *, user_query):
        self.calls.append((job, user_query))
        return self.batch


def _inputs():
    article_id = uuid4()
    job = QualityEvaluationJob(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=[article_id],
    )
    hydrated = HydratedArticleBatch(
        job=job,
        user_query="HPV 疫苗是否有效？",
        articles=[
            EvaluationArticle(
                article_id=article_id,
                title="HPV vaccine trial",
                abstract="Participants were randomized and outcomes were reported.",
            )
        ],
    )
    quality = RCTQualityAssessment(
        article_id=article_id,
        study_type=StudyType.RCT,
        criteria=[
            {
                "criterion_id": criterion,
                "status": CriterionStatus.FAVORABLE,
                "score": 1.0,
                "evidence_quote": "Participants were randomized and outcomes were reported.",
                "reason": "摘要报告了该信息。",
                "evidence_source": EvidenceSource.ABSTRACT,
            }
            for criterion in RCTCriterionId
        ],
        strengths=[],
        limitations=[],
        evidence_gaps=[],
        confidence=0.9,
    )
    relevance = RelevanceAssessment(
        article_id=article_id,
        score=9.0,
        level=RelevanceLevel.HIGH,
        reason="直接相关。",
        answerability_score=8.0,
        answerability_reason="报告主要结果。",
    )
    assessment = ArticleAssessmentBatch(
        task_id=job.task_id,
        group_id=job.group_id,
        study_type=job.study_type,
        article_ids=[article_id],
        relevance_assessments=[relevance],
        quality_assessments=[quality],
    )
    passed_quality = [
        QualityVerificationItem(
            criterion_id=criterion.value,
            status=VerificationStatus.PASSED,
            original_status=CriterionStatus.FAVORABLE,
            original_score=1.0,
            corrected_status=CriterionStatus.FAVORABLE,
            corrected_score=1.0,
            evidence_quote_valid=True,
            reason_supported=True,
            verification_evidence="Participants were randomized and outcomes were reported.",
        )
        for criterion in RCTCriterionId
    ]
    article_verification = ArticleVerification(
        article_id=article_id,
        original_total_score=9.0,
        status=VerificationStatus.PASSED,
        relevance=RelevanceVerificationItem(
            status=VerificationStatus.PASSED,
            original_level=RelevanceLevel.HIGH,
            original_score=9.0,
            corrected_level=RelevanceLevel.HIGH,
            corrected_score=9.0,
            evidence_quote_valid=True,
            reason_supported=True,
            verification_evidence="HPV vaccine trial",
        ),
        answerability=AnswerabilityVerificationItem(
            status=VerificationStatus.PASSED,
            original_score=8.0,
            corrected_score=8.0,
            reason_supported=True,
            verification_evidence="outcomes were reported",
        ),
        quality_items=passed_quality,
    )
    verification = VerificationBatch(
        task_id=job.task_id,
        group_id=job.group_id,
        study_type=job.study_type,
        article_ids=[article_id],
        article_verifications=[article_verification],
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
                    reason="直接研究。",
                )
            ],
        ),
        joint_query_coverage=JointQueryCoverage(
            status=CoverageStatus.DIRECT,
            evidence_article_ids=[article_id],
            reason="单一核心概念被直接覆盖。",
        ),
        coverage_gaps=[],
        redundancy_and_conflicts=[],
        article_contributions=[
            ArticleContribution(
                article_id=article_id,
                role=ContributionRole.CORE_EVIDENCE,
                covered_concepts=["HPV 疫苗"],
                coverage_contribution_score=9.0,
                reason="核心直接证据。",
            )
        ],
        recommendation="保留。",
    )
    return article_id, job, hydrated, assessment, verification, portfolio


@pytest.mark.anyio
async def test_runtime_calls_article_verification_and_portfolio_models() -> None:
    article_id, job, hydrated, assessment, verification, portfolio = _inputs()
    model = FakeStructuredModel([assessment, verification, portfolio])
    repository = FakeRepository(hydrated)
    runtime = QualityEvaluatorRuntime(model=model, repository=repository)

    assessed = await runtime.evaluate(job, user_query=hydrated.user_query)
    verified = await runtime.verify(
        hydrated=hydrated,
        original=assessment,
        original_total_scores={article_id: 9.0},
    )
    evaluated_portfolio = await runtime.evaluate_portfolio(
        task_id=job.task_id,
        user_query=hydrated.user_query,
        intent_analysis={"required_concepts": ["HPV 疫苗"]},
        search_plan={},
        candidate_articles=[
            {
                **hydrated.articles[0].model_dump(mode="json"),
                "corrected_composite_score": 9.0,
                "verification_issues": [],
            }
        ],
    )

    assert assessed == assessment
    assert verified == verification
    assert evaluated_portfolio == portfolio
    assert model.schemas == [
        article_assessment_model_for(StudyType.RCT),
        VerificationBatch,
        PortfolioEvaluation,
    ]
    assert len(model.message_calls) == 3
    for messages in model.message_calls:
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)
        assert isinstance(json.loads(messages[1].content), dict)


@pytest.mark.anyio
async def test_runtime_retries_invalid_structured_response_once() -> None:
    _, job, hydrated, assessment, _, _ = _inputs()
    model = FakeStructuredModel([{"invalid": True}, assessment])
    runtime = QualityEvaluatorRuntime(
        model=model,
        repository=FakeRepository(hydrated),
        max_attempts=2,
    )

    result = await runtime.evaluate(job, user_query=hydrated.user_query)

    assert result == assessment
    assert len(model.message_calls) == 2


@pytest.mark.anyio
async def test_runtime_rejects_verifier_that_rewrites_original_total_score() -> None:
    article_id, _, hydrated, assessment, verification, _ = _inputs()
    payload = verification.model_dump(mode="json")
    payload["article_verifications"][0]["original_total_score"] = 1.0
    rewritten = VerificationBatch.model_validate(payload)
    runtime = QualityEvaluatorRuntime(
        model=FakeStructuredModel([rewritten]),
        repository=FakeRepository(hydrated),
    )

    with pytest.raises(ValueError, match="original_total_score"):
        await runtime.verify(
            hydrated=hydrated,
            original=assessment,
            original_total_scores={article_id: 9.0},
        )
