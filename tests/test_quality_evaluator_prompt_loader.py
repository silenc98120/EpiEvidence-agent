from __future__ import annotations

import pytest

from skills.quality_evaluator.prompt_loader import QualityPromptLoader
from skills.quality_evaluator.rubric_registry import RUBRIC_REGISTRY, get_rubric
from skills.quality_evaluator.schema import StudyType


def test_registry_has_one_nonempty_rubric_for_every_study_type() -> None:
    assert set(RUBRIC_REGISTRY) == set(StudyType)

    for study_type in StudyType:
        rubric = get_rubric(study_type)
        assert rubric.study_type == study_type
        assert rubric.domain.strip()
        assert rubric.rules.strip()


def test_article_prompt_replaces_only_fixed_runtime_markers() -> None:
    loader = QualityPromptLoader()

    prompt = loader.article_evaluation_prompt(StudyType.RCT)

    assert "__STUDY_DOMAIN__" not in prompt
    assert "__RUBRIC_RULES__" not in prompt
    assert "__OUTPUT_SCHEMA__" not in prompt
    assert "rct_pico_clarity" in prompt
    assert "cohort_pico_and_population" not in prompt
    assert '"relevance_assessments"' in prompt
    assert '"quality_assessments"' in prompt


def test_verification_and_portfolio_prompts_receive_their_own_schema() -> None:
    loader = QualityPromptLoader()

    verification = loader.verification_prompt()
    portfolio = loader.portfolio_prompt()

    assert "__OUTPUT_SCHEMA__" not in verification
    assert '"article_verifications"' in verification
    assert '"corrected_status"' in verification
    assert "__OUTPUT_SCHEMA__" not in portfolio
    assert '"article_contributions"' in portfolio
    assert '"coverage_contribution_score"' in portfolio


def test_registry_rejects_unvalidated_user_values() -> None:
    with pytest.raises((TypeError, ValueError)):
        get_rubric("../../secrets" )  # type: ignore[arg-type]
