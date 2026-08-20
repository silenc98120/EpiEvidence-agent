"""In-memory task state storage for the local development API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.api.schemas import TaskCreateRequest, TaskError, TaskResponse, TaskStatus, TaskStage


class TaskStore:
    """Store task snapshots safely within one API process."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, TaskResponse] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: TaskCreateRequest) -> TaskResponse:
        """Create and return a task in the initial ``created`` state."""

        now = datetime.now(timezone.utc)
        task = TaskResponse(
            task_id=uuid4(),
            conversation_id=request.conversation_id,
            query=request.query,
            status=TaskStatus.CREATED,
            stage=TaskStage.CREATED,
            result=None,
            error=None,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task.task_id] = task
        return task

    async def get(self, task_id: UUID) -> TaskResponse | None:
        """Return a task snapshot, or ``None`` when it does not exist."""

        async with self._lock:
            return self._tasks.get(task_id)

    async def list(self) -> list[TaskResponse]:
        """Return all tasks with the newest task first."""

        async with self._lock:
            return sorted(
                self._tasks.values(),
                key=lambda task: task.created_at,
                reverse=True,
            )

    async def update(
        self,
        task_id: UUID,
        *,
        status: TaskStatus | None = None,
        stage: TaskStage | None = None,
        result: dict | None = None,
        error: TaskError | None = None,
    ) -> TaskResponse | None:
        """Update supplied fields and return the new snapshot."""

        async with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                return None

            updated = current.model_copy(
                update={
                    "status": status if status is not None else current.status,
                    "stage": stage if stage is not None else current.stage,
                    "result": result if result is not None else current.result,
                    "error": error if error is not None else current.error,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self._tasks[task_id] = updated
            return updated
