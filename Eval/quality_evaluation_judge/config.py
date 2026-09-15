"""非敏感 YAML 默认值与独立 Judge 环境变量。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_CONFIG_PATH = Path(__file__).parents[1] / "config" / "evaluation.yaml"


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    mode: str = "direct_review"
    temperature: float = 0
    max_concurrency: int = Field(default=1, ge=1)
    max_retries: int = Field(default=2, ge=0)
    rubric_contract_version: str
    prompt_versions: dict[str, str]
    judge_provider: str | None = None
    judge_model: str | None = None
    judge_base_url: str | None = None


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> EvaluationConfig:
    with path.open("r", encoding="utf-8") as stream:
        raw: dict[str, Any] = yaml.safe_load(stream) or {}
    raw.update(
        {
            key: value
            for key, value in {
                "judge_provider": os.getenv("JUDGE_PROVIDER"),
                "judge_model": os.getenv("JUDGE_MODEL"),
                "judge_base_url": os.getenv("JUDGE_BASE_URL"),
            }.items()
            if value
        }
    )
    return EvaluationConfig.model_validate(raw)
