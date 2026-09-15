"""组合仓储快照；保留该边界便于后续脱敏和不可变落盘。"""

from __future__ import annotations

from uuid import UUID

from .repository import EvaluationReadRepository
from ..schemas import TaskEvaluationSnapshot


async def build_task_snapshot(repository: EvaluationReadRepository, task_id: UUID) -> TaskEvaluationSnapshot:
    return await repository.load_task(task_id)
