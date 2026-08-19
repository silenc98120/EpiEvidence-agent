"""加载固定 Prompt 并注入受控 Rubric 与 JSON Schema。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .rubric_registry import get_rubric
from .schema import (
    PortfolioEvaluation,
    StudyType,
    VerificationBatch,
    article_assessment_model_for,
)


_RUNTIME_MARKER = re.compile(r"__[A-Z_]+__")


class QualityPromptLoader:
    def __init__(self, prompt_dir: Path | None = None) -> None:
        self.prompt_dir = prompt_dir or Path(__file__).with_name("prompts")

    def article_evaluation_prompt(self, study_type: StudyType) -> str:
        rubric = get_rubric(study_type)
        return self._render(
            "core_prompt.md",
            {
                "__STUDY_DOMAIN__": rubric.domain,
                "__RUBRIC_RULES__": rubric.rules,
                "__OUTPUT_SCHEMA__": self._schema_json(
                    article_assessment_model_for(study_type)
                ),
            },
        )

    def verification_prompt(self) -> str:
        return self._render(
            "verification_prompt.md",
            {"__OUTPUT_SCHEMA__": self._schema_json(VerificationBatch)},
        )

    def portfolio_prompt(self) -> str:
        return self._render(
            "portfolio_evaluation_prompt.md",
            {"__OUTPUT_SCHEMA__": self._schema_json(PortfolioEvaluation)},
        )

    def _render(self, filename: str, replacements: dict[str, str]) -> str:
        template = (self.prompt_dir / filename).read_text(encoding="utf-8")
        for marker, value in replacements.items():
            if template.count(marker) != 1:
                raise ValueError(f"Prompt {filename} 的 marker {marker} 数量必须为 1")
            template = template.replace(marker, value)
        unresolved = sorted(set(_RUNTIME_MARKER.findall(template)))
        if unresolved:
            raise ValueError(f"Prompt {filename} 存在未替换 marker: {unresolved}")
        return template

    @staticmethod
    def _schema_json(model: Any) -> str:
        return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)
