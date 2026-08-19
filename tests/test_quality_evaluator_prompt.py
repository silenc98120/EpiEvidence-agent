import re
from pathlib import Path
from xml.etree import ElementTree


PROMPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "quality_evaluator"
    / "prompts"
    / "core_prompt.md"
)


def test_core_prompt_has_required_xml_sections() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    root = ElementTree.fromstring(prompt)

    required_tags = (
        "quality_evaluation_prompt",
        "role",
        "task",
        "domain",
        "rules",
        "output_structure",
        "constraints",
        "tips",
    )
    assert root.tag == "quality_evaluation_prompt"
    for tag in required_tags:
        assert prompt.count(f"<{tag}>") == 1
        assert prompt.count(f"</{tag}>") == 1


def test_core_prompt_exposes_only_runtime_injection_markers() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    expected_markers = {
        "__STUDY_DOMAIN__",
        "__RUBRIC_RULES__",
        "__OUTPUT_SCHEMA__",
    }
    assert set(re.findall(r"__[A-Z_]+__", prompt)) == expected_markers
    assert prompt.count("__STUDY_DOMAIN__") == 1
    assert prompt.count("__RUBRIC_RULES__") == 1
    assert prompt.count("__OUTPUT_SCHEMA__") == 1


def test_core_prompt_preserves_scoring_and_provenance_contracts() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    for contract in (
        "favorable = +1",
        "unclear = -0.5",
        "not_reported = -1",
        "not_applicable",
        "evidence_quote",
        "evidence_source",
        "article_id",
        "UUID",
        "preliminary_quality",
    ):
        assert contract in prompt
