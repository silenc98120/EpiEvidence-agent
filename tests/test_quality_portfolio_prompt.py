from pathlib import Path
from xml.etree import ElementTree
import re


PROMPT_PATH = (
    Path(__file__).parents[1]
    / "skills"
    / "quality_evaluator"
    / "prompts"
    / "portfolio_evaluation_prompt.md"
)


def test_portfolio_prompt_has_required_xml_sections() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    root = ElementTree.fromstring(prompt)

    for tag in (
        "portfolio_evaluation_prompt",
        "role",
        "task",
        "inputs",
        "evaluation_scope",
        "rules",
        "output_structure",
        "constraints",
        "tips",
    ):
        assert prompt.count(f"<{tag}>") == 1
        assert prompt.count(f"</{tag}>") == 1
    assert root.tag == "portfolio_evaluation_prompt"


def test_portfolio_prompt_has_only_expected_runtime_markers() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    expected = {
        "__OUTPUT_SCHEMA__",
    }
    assert set(re.findall(r"__[A-Z_]+__", prompt)) == expected


def test_portfolio_prompt_preserves_query_conditioned_coverage_rules() -> None:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for contract in (
        "required_concepts",
        "explicit_constraints",
        "constraint_coverage",
        "keyword_coverage",
        "joint_query_coverage",
        "direct",
        "partial",
        "indirect",
        "absent",
        "conflicting",
        "未指定时间",
        "未指定研究类型",
        "article_id",
        "UUID",
        "不要修改单篇文章的评分",
        "coverage_contribution_score",
        "0 到 10",
    ):
        assert contract in prompt
