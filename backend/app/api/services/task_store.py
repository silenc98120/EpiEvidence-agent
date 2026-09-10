"""In-memory task state storage for the local development API."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4
from typing import Any

from backend.app.api.schemas import TaskCreateRequest, TaskError, TaskResponse, TaskStatus, TaskStage


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

    async def delete(self, task_id: UUID) -> bool:
        """删除尚未被接受执行的临时任务快照。"""

        async with self._lock:
            return self._tasks.pop(task_id, None) is not None

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


class RedisTaskStore:
    """Redis-backed task snapshots for deployments that need process durability.

    The store keeps one JSON document per task and a sorted-set index for listing.
    It deliberately exposes the same async surface as ``TaskStore`` so
    ``TaskRuntime`` and the API routes do not depend on a concrete storage engine.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        redis_url: str | None = None,
        namespace: str = "epi:evidence:tasks",
    ) -> None:
        self.namespace = namespace.strip() or "epi:evidence:tasks"
        self._owns_client = client is None
        if client is not None:
            self.client = client
            return
        from redis.asyncio import Redis

        self.client = Redis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )

    async def create(self, request: TaskCreateRequest) -> TaskResponse:
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
        await self._write(task)
        return task

    async def get(self, task_id: UUID) -> TaskResponse | None:
        raw = await self._call("get", self._task_key(task_id))
        if raw is None:
            return None
        try:
            return TaskResponse.model_validate_json(raw)
        except (TypeError, ValueError):
            return None

    async def list(self) -> list[TaskResponse]:
        ids = await self._call("zrevrange", self._index_key(), 0, -1)
        if not isinstance(ids, (list, tuple)):
            return []
        tasks = [task for task_id in ids if (task := await self.get(UUID(str(task_id))))]
        return tasks

    async def delete(self, task_id: UUID) -> bool:
        """删除未成功持久化到研究事实表的临时任务快照。"""

        deleted = await self._call("delete", self._task_key(task_id))
        await self._call("zrem", self._index_key(), str(task_id))
        return bool(deleted)

    async def update(
        self,
        task_id: UUID,
        *,
        status: TaskStatus | None = None,
        stage: TaskStage | None = None,
        result: dict | None = None,
        error: TaskError | None = None,
    ) -> TaskResponse | None:
        current = await self.get(task_id)
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
        await self._write(updated)
        return updated

    async def close(self) -> None:
        if not self._owns_client:
            return
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _write(self, task: TaskResponse) -> None:
        encoded = task.model_dump_json()
        await self._call("set", self._task_key(task.task_id), encoded)
        await self._call(
            "zadd",
            self._index_key(),
            {str(task.task_id): task.created_at.timestamp()},
        )

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self.client, method_name)
        result = method(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    def _task_key(self, task_id: UUID) -> str:
        return f"{self.namespace}:task:{task_id}"

    def _index_key(self) -> str:
        return f"{self.namespace}:index"
