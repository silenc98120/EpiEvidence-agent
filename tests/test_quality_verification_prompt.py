from pathlib import Path
from xml.etree import ElementTree
import re


PROMPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "quality_evaluator"
    / "prompts"
    / "verification_prompt.md"
)


def test_verification_prompt_has_required_xml_sections() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    root = ElementTree.fromstring(prompt)

    for tag in (
        "quality_verification_prompt",
        "role",
        "task",
        "inputs",
        "verification_procedure",
        "rules",
        "output_structure",
        "constraints",
        "tips",
    ):
        assert prompt.count(f"<{tag}>") == 1
        assert prompt.count(f"</{tag}>") == 1
    assert root.tag == "quality_verification_prompt"


def test_verification_prompt_has_only_output_schema_marker() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    assert set(re.findall(r"__[A-Z_]+__", prompt)) == {"__OUTPUT_SCHEMA__"}


def test_verification_prompt_preserves_audit_contract() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for contract in (
        "article_id",
        "UUID",
        "original_total_score",
        "original_status",
        "original_score",
        "corrected_status",
        "corrected_score",
        "evidence_quote_valid",
        "reason_supported",
        "answerability",
        "favorable = +1",
        "unclear = -0.5",
        "not_reported = -1",
        "not_applicable",
        "不存在 0 分",
        "不要计算 corrected_composite_score",
        "corrected_composite_score &lt;= 3",
        "corrected_composite_score &gt; 3",
    ):
        assert contract in prompt
