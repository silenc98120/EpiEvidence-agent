from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.skills.quality_evaluator.schema import QualityEvaluationJob


class QualityBatchMessage(BaseModel):
    """Celery 质量评估任务的消息契约。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    batch_id: str = Field(min_length=1)
    job: QualityEvaluationJob