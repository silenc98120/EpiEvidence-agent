"""按研究类型按需加载 Judge Prompt，带内存缓存与 SHA-256 签名。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.skills.quality_evaluator.rubric_registry import get_rubric
from backend.skills.quality_evaluator.schema import StudyType

from .enums import JudgeType
from .schemas import CriterionJudgeOutput, RankingJudgeOutput, RoutingJudgeOutput
from ..prompts.study_type_definitions import get_study_type_definition


_RUNTIME_MARKER = re.compile(r"__[A-Z_]+__")


class LoadedPrompt(BaseModel):
    """渲染完成的 Prompt 及审计元数据。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    sha256: str
    judge_type: str  # JudgeType value
    study_type: str  # StudyType value
    prompt_version: str


# ---------------------------------------------------------------------------
# Prompt 模板文件名与占位符映射
# ---------------------------------------------------------------------------

_TEMPLATE_FILES: dict[JudgeType, str] = {
    JudgeType.ROUTING: "routing_judge.md",
    JudgeType.CRITERION: "criterion_judge.md",
    JudgeType.RANKING: "ranking_judge.md",
}

_OUTPUT_SCHEMAS: dict[JudgeType, type[BaseModel]] = {
    JudgeType.ROUTING: RoutingJudgeOutput,
    JudgeType.CRITERION: CriterionJudgeOutput,
    JudgeType.RANKING: RankingJudgeOutput,
}


class JudgePromptLoader:
    """按需加载并渲染 Judge Prompt，按 (JudgeType, StudyType) 缓存。"""

    def __init__(
        self,
        prompt_dir: Path | None = None,
        prompt_versions: dict[str, str] | None = None,
    ) -> None:
        self._prompt_dir = prompt_dir or Path(__file__).parents[1] / "prompts"
        self._prompt_versions = prompt_versions or {}
        self._cache: dict[tuple[JudgeType, StudyType], LoadedPrompt] = {}

    # -- 公开 API ----------------------------------------------------------

    def get_prompt(self, judge_type: JudgeType, study_type: StudyType) -> LoadedPrompt:
        """按需获取指定 Judge × 研究类型的渲染 Prompt（带缓存）。"""
        cache_key = (judge_type, study_type)
        if cache_key in self._cache:
            return self._cache[cache_key]

        if judge_type == JudgeType.ROUTING:
            loaded = self._render_routing(study_type)
        elif judge_type == JudgeType.CRITERION:
            loaded = self._render_criterion(study_type)
        elif judge_type == JudgeType.RANKING:
            loaded = self._render_ranking(study_type)
        else:
            raise ValueError(f"不支持的 JudgeType: {judge_type}")

        self._cache[cache_key] = loaded
        return loaded

    # -- 渲染各 Judge -------------------------------------------------------

    def _render_routing(self, study_type: StudyType) -> LoadedPrompt:
        definition = get_study_type_definition(study_type)
        return self._build(
            JudgeType.ROUTING,
            study_type,
            {
                "__STUDY_TYPE__": study_type.value,
                "__STUDY_TYPE_DEFINITION__": (
                    f"{definition.chinese_name}\n\n"
                    f"{definition.definition}\n\n"
                    f"关键鉴别特征：\n{definition.key_features}"
                ),
                "__CONFUSION_BOUNDARIES__": definition.confusion_boundaries,
                "__OUTPUT_SCHEMA__": self._schema_json(RoutingJudgeOutput),
            },
        )

    def _render_criterion(self, study_type: StudyType) -> LoadedPrompt:
        rubric = get_rubric(study_type)
        return self._build(
            JudgeType.CRITERION,
            study_type,
            {
                "__STUDY_DOMAIN__": rubric.domain,
                "__RUBRIC_RULES__": rubric.rules,
                "__OUTPUT_SCHEMA__": self._schema_json(CriterionJudgeOutput),
            },
        )

    def _render_ranking(self, study_type: StudyType) -> LoadedPrompt:
        definition = get_study_type_definition(study_type)
        return self._build(
            JudgeType.RANKING,
            study_type,
            {
                "__STUDY_TYPE__": study_type.value,
                "__BIAS_PRIORITY__": definition.bias_priority,
                "__OUTPUT_SCHEMA__": self._schema_json(RankingJudgeOutput),
            },
        )

    # -- 内部工具 -----------------------------------------------------------

    def _build(
        self,
        judge_type: JudgeType,
        study_type: StudyType,
        replacements: dict[str, str],
    ) -> LoadedPrompt:
        template_file = _TEMPLATE_FILES[judge_type]
        template = (self._prompt_dir / template_file).read_text(encoding="utf-8")
        rendered = self._render(template_file, template, replacements)
        sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        return LoadedPrompt(
            text=rendered,
            sha256=sha256,
            judge_type=judge_type.value,
            study_type=study_type.value,
            prompt_version=self._prompt_versions.get(judge_type.value, "unversioned"),
        )

    @staticmethod
    def _render(filename: str, template: str, replacements: dict[str, str]) -> str:
        for marker, value in replacements.items():
            count = template.count(marker)
            if count == 0:
                raise ValueError(f"Prompt {filename} 缺少 marker {marker}")
            if count > 1:
                raise ValueError(f"Prompt {filename} 的 marker {marker} 出现 {count} 次，应为 1")
            template = template.replace(marker, value)
        unresolved = sorted(set(_RUNTIME_MARKER.findall(template)))
        if unresolved:
            raise ValueError(f"Prompt {filename} 存在未替换 marker: {unresolved}")
        return template

    @staticmethod
    def _schema_json(model: Any) -> str:
        return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2)
