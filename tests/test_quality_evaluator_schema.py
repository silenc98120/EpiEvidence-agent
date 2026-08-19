from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from skills.quality_evaluator.schema import (
    CRITERION_ENUM_BY_STUDY_TYPE,
    QUALITY_MODEL_BY_STUDY_TYPE,
    ArticleAssessmentBatch,
    CriterionStatus,
    EvidenceSource,
    EvaluationArticle,
    HydratedArticleBatch,
    QualityEvaluationJob,
    RCTCriterionAssessment,
    RCTCriterionId,
    RCTQualityAssessment,
    RelevanceAssessment,
    RelevanceLevel,
    StudyType,
    ArticleContribution,
    ArticleVerification,
    AnswerabilityVerificationItem,
    ContributionRole,
    CoverageAssessment,
    CoverageStatus,
    JointQueryCoverage,
    PortfolioEvaluation,
    QualityVerificationItem,
    QueryCoverage,
    RelevanceVerificationItem,
    VerificationBatch,
    VerificationStatus,
    article_assessment_model_for,
)


def _criterion_payload(criterion_id: str) -> dict[str, object]:
    return {
        "criterion_id": criterion_id,
        "status": CriterionStatus.FAVORABLE,
        "score": 1.0,
        "evidence_quote": "Randomized trial with clearly reported outcomes.",
        "reason": "摘要明确报告了支持该条目的信息。",
        "evidence_source": EvidenceSource.ABSTRACT,
    }


def _quality_payload(study_type: StudyType, article_id: UUID) -> dict[str, object]:
    criterion_enum = CRITERION_ENUM_BY_STUDY_TYPE[study_type]
    return {
        "article_id": article_id,
        "study_type": study_type,
        "criteria": [_criterion_payload(item.value) for item in criterion_enum],
        "strengths": ["摘要报告较完整。"],
        "limitations": [],
        "evidence_gaps": [],
        "confidence": 0.9,
    }


def _relevance(article_id: UUID, score: float = 9.0) -> RelevanceAssessment:
    return RelevanceAssessment(
        article_id=article_id,
        score=score,
        level=RelevanceLevel.HIGH,
        reason="研究问题与用户 query 直接相关。",
        answerability_score=8.0,
        answerability_reason="摘要报告了主要结局、效应方向和置信区间。",
        matched_dimensions=["population", "outcome"],
        evidence_gaps=[],
    )


def test_hydrated_batch_requires_exact_unique_canonical_uuids() -> None:
    first_id = uuid4()
    second_id = uuid4()
    job = QualityEvaluationJob(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=[first_id, second_id],
    )

    batch = HydratedArticleBatch(
        job=job,
        user_query="HPV 疫苗两剂是否有效？",
        articles=[
            EvaluationArticle(article_id=second_id, title="Second", abstract="Abstract"),
            EvaluationArticle(article_id=first_id, title="First", abstract="Abstract"),
        ],
    )

    assert {article.article_id for article in batch.articles} == {first_id, second_id}

    with pytest.raises(ValidationError, match="数据库返回的文章 UUID 必须与任务完全一致"):
        HydratedArticleBatch(
            job=job,
            user_query="HPV 疫苗两剂是否有效？",
            articles=[EvaluationArticle(article_id=first_id, title="First")],
        )

    with pytest.raises(ValidationError, match="article_ids 不能重复"):
        QualityEvaluationJob(
            task_id="task-1",
            group_id="rct-1",
            study_type=StudyType.RCT,
            article_ids=[first_id, first_id],
        )


@pytest.mark.parametrize(
    ("score", "level"),
    [
        (8.0, RelevanceLevel.HIGH),
        (4.0, RelevanceLevel.PARTIAL),
        (3.0, RelevanceLevel.LOW),
    ],
)
def test_relevance_level_must_match_score(score: float, level: RelevanceLevel) -> None:
    assessment = RelevanceAssessment(
        article_id=uuid4(),
        score=score,
        level=level,
        reason="评分与用户 query 的匹配程度一致。",
        answerability_score=8.0,
        answerability_reason="摘要提供了可用于回答问题的结果。",
    )

    assert assessment.level == level

    with pytest.raises(ValidationError, match="相关性等级与分数不一致"):
        RelevanceAssessment(
            article_id=uuid4(),
            score=score,
            level=(
                RelevanceLevel.LOW
                if level != RelevanceLevel.LOW
                else RelevanceLevel.HIGH
            ),
            reason="错误等级。",
            answerability_score=8.0,
            answerability_reason="摘要提供了可用于回答问题的结果。",
        )


def test_relevance_requires_answerability_score_and_reason() -> None:
    with pytest.raises(ValidationError):
        RelevanceAssessment(
            article_id=uuid4(),
            score=9.0,
            level=RelevanceLevel.HIGH,
            reason="研究问题直接相关。",
        )


def test_criterion_score_and_missing_evidence_marker_are_validated() -> None:
    favorable = RCTCriterionAssessment(
        criterion_id=RCTCriterionId.PICO_CLARITY,
        status=CriterionStatus.FAVORABLE,
        score=1.0,
        evidence_quote="Participants were randomized 1:1.",
        reason="摘要明确报告随机分组。",
        evidence_source=EvidenceSource.ABSTRACT,
    )
    assert favorable.score == 1.0

    with pytest.raises(ValidationError, match="status 对应的固定分值"):
        RCTCriterionAssessment(
            criterion_id=RCTCriterionId.PICO_CLARITY,
            status=CriterionStatus.UNCLEAR,
            score=1.0,
            evidence_quote="Randomized trial.",
            reason="信息不足。",
            evidence_source=EvidenceSource.ABSTRACT,
        )

    with pytest.raises(ValidationError, match="摘要未报告"):
        RCTCriterionAssessment(
            criterion_id=RCTCriterionId.PICO_CLARITY,
            status=CriterionStatus.NOT_REPORTED,
            score=-1.0,
            evidence_quote="No evidence",
            reason="摘要没有相关信息。",
            evidence_source=EvidenceSource.ABSTRACT,
        )


@pytest.mark.parametrize(
    ("study_type", "quality_model"),
    list(QUALITY_MODEL_BY_STUDY_TYPE.items()),
)
def test_each_study_type_requires_its_complete_rubric(
    study_type: StudyType,
    quality_model: type,
) -> None:
    article_id = uuid4()
    payload = _quality_payload(study_type, article_id)

    assessment = quality_model.model_validate(payload)

    assert assessment.study_type == study_type
    assert len(assessment.criteria) == len(CRITERION_ENUM_BY_STUDY_TYPE[study_type])

    payload["criteria"] = payload["criteria"][:-1]  # type: ignore[index]
    with pytest.raises(ValidationError, match="criteria"):
        quality_model.model_validate(payload)


def test_article_batch_merges_two_assessment_parts_by_uuid() -> None:
    first_id = uuid4()
    second_id = uuid4()
    article_ids = [first_id, second_id]
    quality_assessments = [
        RCTQualityAssessment.model_validate(_quality_payload(StudyType.RCT, article_id))
        for article_id in article_ids
    ]

    batch = ArticleAssessmentBatch(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=article_ids,
        relevance_assessments=[_relevance(second_id), _relevance(first_id)],
        quality_assessments=quality_assessments,
    )

    assert {item.article_id for item in batch.relevance_assessments} == set(article_ids)
    assert {item.article_id for item in batch.quality_assessments} == set(article_ids)

    with pytest.raises(ValidationError, match="两个评价部分必须完整覆盖 article_ids"):
        ArticleAssessmentBatch(
            task_id="task-1",
            group_id="rct-1",
            study_type=StudyType.RCT,
            article_ids=article_ids,
            relevance_assessments=[_relevance(first_id)],
            quality_assessments=quality_assessments,
        )


def test_article_batch_rejects_quality_model_from_another_study_type() -> None:
    article_id = uuid4()
    cohort_model = QUALITY_MODEL_BY_STUDY_TYPE[StudyType.COHORT]

    with pytest.raises(ValidationError, match="质量评价类型必须与批次 study_type 一致"):
        ArticleAssessmentBatch(
            task_id="task-1",
            group_id="rct-1",
            study_type=StudyType.RCT,
            article_ids=[article_id],
            relevance_assessments=[_relevance(article_id)],
            quality_assessments=[
                cohort_model.model_validate(_quality_payload(StudyType.COHORT, article_id))
            ],
        )


def test_article_batch_json_schema_exposes_study_type_discriminator() -> None:
    schema = ArticleAssessmentBatch.model_json_schema()
    quality_items = schema["properties"]["quality_assessments"]["items"]

    assert quality_items["discriminator"]["propertyName"] == "study_type"
    assert len(quality_items["oneOf"]) == len(QUALITY_MODEL_BY_STUDY_TYPE)


def test_study_specific_batch_schema_exposes_only_current_quality_model() -> None:
    schema = article_assessment_model_for(StudyType.RCT).model_json_schema()
    serialized = str(schema)

    assert "RCTQualityAssessment" in serialized
    assert "CohortQualityAssessment" not in serialized


def _relevance_verification(
    *,
    status: VerificationStatus = VerificationStatus.PASSED,
) -> RelevanceVerificationItem:
    return RelevanceVerificationItem(
        status=status,
        original_level=RelevanceLevel.HIGH,
        original_score=9.0,
        corrected_level=(
            RelevanceLevel.HIGH
            if status == VerificationStatus.PASSED
            else RelevanceLevel.PARTIAL
        ),
        corrected_score=9.0 if status == VerificationStatus.PASSED else 6.0,
        evidence_quote_valid=True,
        reason_supported=status == VerificationStatus.PASSED,
        issue=None if status == VerificationStatus.PASSED else "相关性理由夸大了人群匹配。",
        verification_evidence="摘要仅纳入一般成年受试者。",
    )


def _quality_verification(
    *,
    status: VerificationStatus = VerificationStatus.PASSED,
    criterion_id: str = RCTCriterionId.PICO_CLARITY.value,
) -> QualityVerificationItem:
    return QualityVerificationItem(
        criterion_id=criterion_id,
        status=status,
        original_status=CriterionStatus.FAVORABLE,
        original_score=1.0,
        corrected_status=(
            CriterionStatus.FAVORABLE
            if status == VerificationStatus.PASSED
            else CriterionStatus.NOT_REPORTED
        ),
        corrected_score=1.0 if status == VerificationStatus.PASSED else -1.0,
        evidence_quote_valid=status == VerificationStatus.PASSED,
        reason_supported=status == VerificationStatus.PASSED,
        issue=None if status == VerificationStatus.PASSED else "摘要没有报告比较组。",
        verification_evidence=(
            "Participants were randomized 1:1."
            if status == VerificationStatus.PASSED
            else "摘要未报告"
        ),
    )


def _answerability_verification(
    *,
    status: VerificationStatus = VerificationStatus.PASSED,
) -> AnswerabilityVerificationItem:
    return AnswerabilityVerificationItem(
        status=status,
        original_score=8.0,
        corrected_score=8.0 if status == VerificationStatus.PASSED else 5.0,
        reason_supported=status == VerificationStatus.PASSED,
        issue=None if status == VerificationStatus.PASSED else "摘要没有报告效应量。",
        verification_evidence="摘要报告结局方向，但没有效应量。",
    )


def test_verification_items_enforce_status_score_and_pass_fail_contracts() -> None:
    assert _relevance_verification().status == VerificationStatus.PASSED
    assert _quality_verification(status=VerificationStatus.FAILED).corrected_score == -1.0

    with pytest.raises(ValidationError, match="质量状态对应的固定分值"):
        QualityVerificationItem(
            criterion_id=RCTCriterionId.PICO_CLARITY.value,
            status=VerificationStatus.FAILED,
            original_status=CriterionStatus.FAVORABLE,
            original_score=1.0,
            corrected_status=CriterionStatus.UNCLEAR,
            corrected_score=0.0,
            evidence_quote_valid=True,
            reason_supported=False,
            issue="证据不足。",
            verification_evidence="Randomized trial.",
        )

    with pytest.raises(ValidationError, match="passed 核查不得改变"):
        RelevanceVerificationItem(
            status=VerificationStatus.PASSED,
            original_level=RelevanceLevel.HIGH,
            original_score=9.0,
            corrected_level=RelevanceLevel.PARTIAL,
            corrected_score=6.0,
            evidence_quote_valid=True,
            reason_supported=True,
            verification_evidence="摘要与 query 匹配。",
        )


def test_verification_batch_covers_each_canonical_uuid_once() -> None:
    article_id = uuid4()
    article = ArticleVerification(
        article_id=article_id,
        original_total_score=7.5,
        status=VerificationStatus.PASSED,
        relevance=_relevance_verification(),
        answerability=_answerability_verification(),
        quality_items=[
            _quality_verification(criterion_id=criterion.value)
            for criterion in RCTCriterionId
        ],
    )
    batch = VerificationBatch(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=[article_id],
        article_verifications=[article],
    )

    assert batch.article_verifications[0].article_id == article_id

    with pytest.raises(ValidationError, match="必须完整覆盖 article_ids"):
        VerificationBatch(
            task_id="task-1",
            group_id="rct-1",
            study_type=StudyType.RCT,
            article_ids=[article_id, uuid4()],
            article_verifications=[article],
        )


def test_answerability_verification_is_independent_from_relevance() -> None:
    failed = _answerability_verification(status=VerificationStatus.FAILED)

    assert failed.corrected_score == 5.0
    with pytest.raises(ValidationError, match="passed 核查不得改变"):
        AnswerabilityVerificationItem(
            status=VerificationStatus.PASSED,
            original_score=8.0,
            corrected_score=5.0,
            reason_supported=True,
            verification_evidence="摘要报告了结果。",
        )


def test_portfolio_evaluation_covers_candidate_uuids_and_scores_contributions() -> None:
    article_id = uuid4()
    evaluation = PortfolioEvaluation(
        task_id="task-1",
        candidate_article_ids=[article_id],
        query_coverage=QueryCoverage(
            required_concepts=["司美格鲁肽", "老年肥胖人群", "心血管疾病"],
            explicit_constraints=[],
            keyword_coverage=[
                CoverageAssessment(
                    concept="司美格鲁肽",
                    status=CoverageStatus.DIRECT,
                    evidence_article_ids=[article_id],
                    reason="摘要明确研究司美格鲁肽。",
                ),
                CoverageAssessment(
                    concept="老年肥胖人群",
                    status=CoverageStatus.PARTIAL,
                    evidence_article_ids=[article_id],
                    reason="摘要纳入肥胖成人，但没有单独报告老年亚组。",
                ),
                CoverageAssessment(
                    concept="心血管疾病",
                    status=CoverageStatus.DIRECT,
                    evidence_article_ids=[article_id],
                    reason="摘要明确报告心血管结局。",
                ),
            ],
            constraint_coverage=[],
        ),
        joint_query_coverage=JointQueryCoverage(
            status=CoverageStatus.PARTIAL,
            evidence_article_ids=[article_id],
            reason="摘要没有单独报告老年亚组。",
        ),
        coverage_gaps=["老年肥胖人群的直接证据不足。"],
        redundancy_and_conflicts=[],
        article_contributions=[
            ArticleContribution(
                article_id=article_id,
                role=ContributionRole.CORE_EVIDENCE,
                covered_concepts=["司美格鲁肽", "心血管疾病"],
                coverage_contribution_score=7.0,
                reason="直接覆盖干预与结局，但人群仅部分匹配。",
            )
        ],
        recommendation="保留为核心但不完整的直接证据。",
    )

    assert evaluation.article_contributions[0].coverage_contribution_score == 7.0

    with pytest.raises(ValidationError, match="必须完整覆盖 candidate_article_ids"):
        PortfolioEvaluation(
            **{
                **evaluation.model_dump(),
                "candidate_article_ids": [article_id, uuid4()],
            }
        )


def test_query_coverage_requires_each_explicit_constraint_once() -> None:
    with pytest.raises(ValidationError, match="constraint_coverage"):
        QueryCoverage(
            required_concepts=["司美格鲁肽"],
            explicit_constraints=["仅限 RCT"],
            keyword_coverage=[
                CoverageAssessment(
                    concept="司美格鲁肽",
                    status=CoverageStatus.ABSENT,
                    evidence_article_ids=[],
                    reason="候选集中没有直接证据。",
                )
            ],
            constraint_coverage=[],
        )
