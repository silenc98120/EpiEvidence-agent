import pytest

from app.core.intent_recognizer import (
    IntentCategory,
    IntentRecognitionResult,
    ResearchDirection,
    ResearchKeywordKind,
)
from app.core.mesh_normalizer import (
    MeshMatch,
    MeshNormalizationResult,
    MeshNormalizedKeyword,
)
from app.core.search_strategy_generator import (
    EuropePMCQueryCompiler,
    PMCQueryCompiler,
    PubMedQueryCompiler,
    SearchPlanBuilder,
    SearchSource,
    SemanticScholarQueryCompiler,
)


def _research_intent() -> IntentRecognitionResult:
    return IntentRecognitionResult(
        intent_category=IntentCategory.NEW_RESEARCH,
        intent_confidence=0.95,
        directions=[ResearchDirection.EFFECTIVENESS],
        keywords={},
        short_reason="用户提出一个新的药物效果研究问题。",
    )


def _mesh_normalization() -> MeshNormalizationResult:
    return MeshNormalizationResult(
        normalized_keywords=[
            MeshNormalizedKeyword(
                kind=ResearchKeywordKind.DRUG,
                raw_text="司美格鲁肽",
                lookup_terms=["Semaglutide", "semaglutide"],
                llm_confidence=0.99,
                mesh_matches=[
                    MeshMatch(
                        mesh_id="D000099194",
                        preferred_label="Semaglutide",
                        matched_term="Semaglutide",
                        matched_term_type="preferred",
                        entry_terms=["Ozempic", "Rybelsus", "Wegovy"],
                    )
                ],
            ),
            MeshNormalizedKeyword(
                kind=ResearchKeywordKind.POPULATION,
                raw_text="肥胖人群",
                lookup_terms=["people with obesity", "obese population"],
                llm_confidence=0.95,
                mesh_matches=[
                    MeshMatch(
                        mesh_id="D009765",
                        preferred_label="Obesity",
                        matched_term="Obesity",
                        matched_term_type="preferred",
                        entry_terms=["Obese"],
                    )
                ],
            ),
            MeshNormalizedKeyword(
                kind=ResearchKeywordKind.OUTCOME,
                raw_text="减重效果",
                lookup_terms=["weight loss", "body weight reduction"],
                llm_confidence=0.93,
                mesh_matches=[],
            ),
        ]
    )


def test_search_plan_builder_groups_mesh_and_free_text_terms() -> None:
    plan = SearchPlanBuilder().build(
        _research_intent(),
        _mesh_normalization(),
    )

    assert len(plan.concept_groups) == 3

    drug_group = plan.concept_groups[0]
    assert drug_group.mesh_headings[0].mesh_id == "D000099194"
    assert drug_group.mesh_headings[0].label == "Semaglutide"
    assert drug_group.free_text_terms == [
        "Semaglutide",
        "Ozempic",
        "Rybelsus",
        "Wegovy",
    ]


def test_compilers_apply_database_specific_field_syntax() -> None:
    plan = SearchPlanBuilder().build(
        _research_intent(),
        _mesh_normalization(),
    )

    pubmed_query = PubMedQueryCompiler().compile(plan).query
    europe_pmc_query = EuropePMCQueryCompiler().compile(plan).query

    assert '"Semaglutide"[Mesh]' in pubmed_query
    assert '"Ozempic"[Title/Abstract]' in pubmed_query
    assert '"weight loss"[Title/Abstract]' in pubmed_query
    assert pubmed_query.count(" AND ") == 2

    assert 'MESH:"Semaglutide"' in europe_pmc_query
    assert 'TITLE_ABS:"Ozempic"' in europe_pmc_query
    assert 'TITLE_ABS:"weight loss"' in europe_pmc_query
    assert europe_pmc_query.count(" AND ") == 2


def test_search_plan_builder_rejects_simple_chat() -> None:
    simple_chat = IntentRecognitionResult(
        intent_category=IntentCategory.SIMPLE_CHAT,
        intent_confidence=0.99,
        directions=[],
        keywords={},
        short_reason="用户正在问候。",
    )

    with pytest.raises(ValueError, match="simple_chat"):
        SearchPlanBuilder().build(simple_chat, MeshNormalizationResult())


def test_supplementary_source_compilers_use_supported_query_syntax() -> None:
    plan = SearchPlanBuilder().build(_research_intent(), _mesh_normalization())

    pmc_query = PMCQueryCompiler().compile(plan)
    semantic_query = SemanticScholarQueryCompiler().compile(plan)

    assert pmc_query.source == SearchSource.PMC
    assert '"Semaglutide"[Mesh]' in pmc_query.query
    assert semantic_query.source == SearchSource.SEMANTIC_SCHOLAR
    assert semantic_query.query == '"Semaglutide" "Obesity" "weight loss"'
    assert "MESH:" not in semantic_query.query
