from pathlib import Path

from app.core.intent_recognizer import (
    IntentCategory,
    IntentRecognitionResult,
    KeywordCandidate,
    ResearchDirection,
    ResearchKeywordKind,
)
from app.core.mesh_normalizer import (
    prepare_keyword_lookups,
    prepare_lookup_terms,
    MeshRepository,
    MeshNormalizer,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MESH_DATABASE_PATH = PROJECT_ROOT / "data" / "mesh.sqlite3"

def test_prepare_keyword_lookups_formats_llm_keywords() -> None:
    intent = IntentRecognitionResult(
        intent_category=IntentCategory.NEW_RESEARCH,
        intent_confidence=0.9,
        directions=[ResearchDirection.EFFECTIVENESS],
        keywords={
            ResearchKeywordKind.DRUG: [
                KeywordCandidate(
                    raw_text="司美格鲁肽",
                    english_candidates=[
                        "Semaglutide",
                        " semaglutide ",
                    ],
                    confidence=0.98,
                )
            ]
        },
        short_reason="用户提出新的药物效果研究问题。",
    )

    lookups = prepare_keyword_lookups(intent)

    assert len(lookups) == 1
    assert lookups[0].kind == ResearchKeywordKind.DRUG
    assert lookups[0].raw_text == "司美格鲁肽"
    assert lookups[0].lookup_terms == [
        "Semaglutide",
    ]
    assert lookups[0].llm_confidence == 0.98

def test_prepare_lookup_terms_removes_duplicates_and_blanks() -> None:
    keyword = KeywordCandidate(
        raw_text="司美格鲁肽",
        english_candidates=[
            "Semaglutide",
            " semaglutide ",
            "   ",
        ],
        confidence=0.98,
    )

    result = prepare_lookup_terms(keyword)

    assert result == [
        "Semaglutide",
    ]
def test_prepare_lookup_terms_returns_empty_without_english_candidates() -> None:
    keyword = KeywordCandidate(
        raw_text="司美格鲁肽",
        english_candidates=[],
        confidence=0.7,
    )

    result = prepare_lookup_terms(keyword)

    assert result == []

def test_mesh_normalizer_matches_intent_keywords() -> None:
    intent = IntentRecognitionResult(
        intent_category=IntentCategory.NEW_RESEARCH,
        intent_confidence=0.9,
        directions=[ResearchDirection.EFFECTIVENESS],
        keywords={
            ResearchKeywordKind.DRUG: [
                KeywordCandidate(
                    raw_text="司美格鲁肽",
                    english_candidates=["Semaglutide"],
                    confidence=0.98,
                )
            ]
        },
        short_reason="用户提出新的药物效果研究问题。",
    )

    repository = MeshRepository(MESH_DATABASE_PATH)
    normalizer = MeshNormalizer(repository)

    result = normalizer.normalize(intent)

    assert len(result.normalized_keywords) == 1

    normalized_keyword = result.normalized_keywords[0]

    assert normalized_keyword.kind == ResearchKeywordKind.DRUG
    assert normalized_keyword.raw_text == "司美格鲁肽"
    assert normalized_keyword.lookup_terms == ["Semaglutide"]
    assert len(normalized_keyword.mesh_matches) == 1

    match = normalized_keyword.mesh_matches[0]

    assert match.mesh_id == "D000099194"
    assert match.preferred_label == "Semaglutide"
    assert set(match.entry_terms) == {
        "Ozempic",
        "Rybelsus",
        "Wegovy",
    }