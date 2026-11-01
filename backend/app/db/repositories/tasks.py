"""研究任务的 PostgreSQL 事实记录边界。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.db.models import TaskORM


class TaskPersistenceUnavailable(RuntimeError):
    """任务事实无法持久化，调用方不得继续调度图或 Worker。"""


class ResearchTaskRepository:
    """维护 API task_id 对应的持久化研究任务。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, task_id: UUID):
        """进程重启后从事实表重建公共快照。运行中任务需显式恢复。"""
        from backend.app.api.schemas import TaskResponse, TaskStatus, TaskStage, TaskError
        from backend.app.api.services.output_adapter import build_public_result
        async with self._session_factory() as session:
            row = await session.get(TaskORM, task_id)
            if row is None:
                return None
            terminal = row.status in {"completed", "failed", "cancelled"}
            status = TaskStatus(row.status) if terminal else TaskStatus.FAILED
            result = None
            if row.final_summary:
                result = build_public_result({"task_id": str(task_id), "user_query": row.user_query,
                    "final_summary": row.final_summary}).model_dump(mode="json")
            return TaskResponse(task_id=task_id, conversation_id=row.conversation_id,
                query=row.user_query, status=status, stage=TaskStage(status.value), result=result,
                error=TaskError(code=row.error_type or "process_interrupted", message="任务执行中断，可从检查点恢复", retryable=True)
                    if status == TaskStatus.FAILED else None,
                created_at=row.created_at, updated_at=row.updated_at)

    async def ensure_task(
        self,
        *,
        task_id: UUID,
        query: str,
        conversation_id: UUID | None,
    ) -> None:
        """创建任务事实；同一 task_id 的重复调用保持幂等。"""

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.get(TaskORM, task_id)
                    if task is None:
                        session.add(
                            TaskORM(
                                task_id=task_id,
                                conversation_id=conversation_id,
                                user_query=query,
                                status="created",
                            )
                        )
                    elif task.user_query != query:
                        raise ValueError("task_id 已关联到不同的研究问题")
        except ValueError:
            raise
        except Exception as exc:
            raise TaskPersistenceUnavailable("研究任务持久化不可用") from exc

    async def mark_running(self, task_id: UUID) -> None:
        await self._update_status(task_id, "recognizing_intent")

    async def mark_completed(self, task_id: UUID) -> None:
        await self._update_status(task_id, "completed", completed=True)

    async def mark_failed(self, task_id: UUID, error_type: str) -> None:
        await self._update_status(task_id, "failed", error_type=error_type)

    async def mark_cancelled(self, task_id: UUID) -> None:
        await self._update_status(task_id, "cancelled")

    async def _update_status(
        self,
        task_id: UUID,
        status: str,
        *,
        error_type: str | None = None,
        completed: bool = False,
    ) -> None:
        values: dict[str, object] = {
            "status": status,
            "error_type": error_type,
            "error_message": None,
        }
        if completed:
            values["completed_at"] = datetime.now(timezone.utc)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    result = await session.execute(
                        update(TaskORM)
                        .where(TaskORM.task_id == task_id)
                        .values(**values)
                    )
                    if result.rowcount != 1:
                        raise LookupError(f"PostgreSQL 未找到 task_id={task_id}")
        except LookupError:
            raise
        except Exception as exc:
            raise TaskPersistenceUnavailable("研究任务状态持久化不可用") from exc
