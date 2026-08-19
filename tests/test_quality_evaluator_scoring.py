from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest

from skills.quality_evaluator.schema import (
    ArticleContribution,
    ArticleVerification,
    AnswerabilityVerificationItem,
    ContributionRole,
    CriterionStatus,
    EvidenceSource,
    QUALITY_MODEL_BY_STUDY_TYPE,
    RCTCriterionId,
    RelevanceAssessment,
    RelevanceLevel,
    RelevanceVerificationItem,
    StudyType,
    VerificationStatus,
    QualityVerificationItem,
)
from skills.quality_evaluator.scoring import (
    ArticleScore,
    calculate_composite_score,
    calculate_preliminary_quality,
    partition_by_threshold,
    rank_candidates,
    rescore_verified_article,
)


def test_quality_normalization_excludes_not_applicable_from_denominator() -> None:
    assert calculate_preliminary_quality([1.0, -0.5, -1.0, None]) == pytest.approx(
        5 + 5 * (-0.5 / 3)
    )

    with pytest.raises(ValueError, match="没有可计分"):
        calculate_preliminary_quality([None, None])


def test_composite_score_uses_configured_five_component_weights() -> None:
    assert calculate_composite_score(
        relevance_score=8.0,
        quality_score=6.0,
        answerability_score=7.0,
        journal_score=4.0,
        recency_score=9.0,
    ) == pytest.approx(7.2)


def _quality(article_id):
    model = QUALITY_MODEL_BY_STUDY_TYPE[StudyType.RCT]
    return model.model_validate(
        {
            "article_id": article_id,
            "study_type": StudyType.RCT,
            "criteria": [
                {
                    "criterion_id": criterion.value,
                    "status": CriterionStatus.FAVORABLE,
                    "score": 1.0,
                    "evidence_quote": "Randomized trial with reported results.",
                    "reason": "摘要报告了该信息。",
                    "evidence_source": EvidenceSource.ABSTRACT,
                }
                for criterion in RCTCriterionId
            ],
            "strengths": [],
            "limitations": [],
            "evidence_gaps": [],
            "confidence": 0.9,
        }
    )


def _verification(article_id) -> ArticleVerification:
    quality_items = []
    for criterion in RCTCriterionId:
        failed = criterion == RCTCriterionId.STATISTICAL_REPORTING
        quality_items.append(
            QualityVerificationItem(
                criterion_id=criterion.value,
                status=VerificationStatus.FAILED if failed else VerificationStatus.PASSED,
                original_status=CriterionStatus.FAVORABLE,
                original_score=1.0,
                corrected_status=(
                    CriterionStatus.NOT_REPORTED if failed else CriterionStatus.FAVORABLE
                ),
                corrected_score=-1.0 if failed else 1.0,
                evidence_quote_valid=not failed,
                reason_supported=not failed,
                issue="摘要没有报告效应量。" if failed else None,
                verification_evidence=(
                    "摘要未报告" if failed else "Randomized trial with reported results."
                ),
            )
        )
    return ArticleVerification(
        article_id=article_id,
        original_total_score=9.0,
        status=VerificationStatus.FAILED,
        relevance=RelevanceVerificationItem(
            status=VerificationStatus.FAILED,
            original_level=RelevanceLevel.HIGH,
            original_score=9.0,
            corrected_level=RelevanceLevel.PARTIAL,
            corrected_score=6.0,
            evidence_quote_valid=True,
            reason_supported=False,
            issue="目标人群仅部分匹配。",
            verification_evidence="摘要纳入一般成人。",
        ),
        answerability=AnswerabilityVerificationItem(
            status=VerificationStatus.FAILED,
            original_score=8.0,
            corrected_score=5.0,
            reason_supported=False,
            issue="摘要没有效应量。",
            verification_evidence="摘要仅报告方向。",
        ),
        quality_items=quality_items,
    )


def test_verification_recomputes_weighted_score_from_corrected_components() -> None:
    article_id = uuid4()
    relevance = RelevanceAssessment(
        article_id=article_id,
        score=9.0,
        level=RelevanceLevel.HIGH,
        reason="研究与 query 高度相关。",
        answerability_score=8.0,
        answerability_reason="摘要报告主要结果。",
    )

    score = rescore_verified_article(
        relevance=relevance,
        quality=_quality(article_id),
        verification=_verification(article_id),
        journal_score=7.0,
        recency_score=9.0,
    )

    assert score.quality_score == 8.0
    assert score.composite_score == pytest.approx(6.55)
    assert len(score.verification_issues) == 3


def _score(article_id: UUID, composite: float) -> ArticleScore:
    return ArticleScore(
        article_id=article_id,
        relevance_score=composite,
        quality_score=composite,
        answerability_score=composite,
        journal_score=composite,
        recency_score=composite,
        composite_score=composite,
        verification_issues=(),
    )


def test_threshold_excludes_three_and_retains_above_three() -> None:
    excluded = _score(uuid4(), 3.0)
    retained = _score(uuid4(), 3.0001)

    decision = partition_by_threshold([retained, excluded])

    assert decision.retained == (retained,)
    assert decision.excluded == (excluded,)


def test_candidate_sort_is_stable_and_uses_four_explicit_keys() -> None:
    first_id = UUID("00000000-0000-0000-0000-000000000001")
    second_id = UUID("00000000-0000-0000-0000-000000000002")
    third_id = UUID("00000000-0000-0000-0000-000000000003")
    scores = [_score(first_id, 8.0), _score(second_id, 9.0), _score(third_id, 8.0)]
    contributions = [
        ArticleContribution(
            article_id=first_id,
            role=ContributionRole.CORE_EVIDENCE,
            covered_concepts=["A"],
            coverage_contribution_score=8.0,
            reason="core",
        ),
        ArticleContribution(
            article_id=second_id,
            role=ContributionRole.CORE_EVIDENCE,
            covered_concepts=["A"],
            coverage_contribution_score=7.0,
            reason="core",
        ),
        ArticleContribution(
            article_id=third_id,
            role=ContributionRole.CORE_EVIDENCE,
            covered_concepts=["A"],
            coverage_contribution_score=8.0,
            reason="core",
        ),
    ]

    ranked = rank_candidates(
        scores,
        contributions,
        publication_dates={
            first_id: date(2025, 1, 1),
            second_id: date(2026, 1, 1),
            third_id: date(2024, 1, 1),
        },
    )

    assert [item.article_id for item in ranked] == [first_id, third_id, second_id]
