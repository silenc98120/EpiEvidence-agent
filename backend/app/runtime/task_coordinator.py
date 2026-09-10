"""协调 API 任务生命周期与 LangGraph 图执行。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from langfuse import Langfuse,propagate_attributes
from loguru import logger

from backend.app.db.repositories import TaskPersistenceUnavailable
from backend.app.api.schemas import TaskError, TaskStage, TaskStatus
from backend.app.api.services.agui_events import (
    public_stage,
    run_cancelled,
    run_error,
    run_finished,
    run_started,
    step_finished,
    step_started,
    text_content,
    text_ended,
    text_started,
)
from backend.app.api.services.event_broker import EventBroker
from backend.app.api.services.output_adapter import build_public_result
from backend.app.api.services.task_store import TaskStore



class TaskPersistence(Protocol):
    """TaskCoordinator 需要的最小持久化任务契约。"""

    async def ensure_task(
        self,
        *,
        task_id: UUID,
        query: str,
        conversation_id: UUID | None,
    ) -> None: ...

    async def mark_running(self, task_id: UUID) -> None: ...

    async def mark_completed(self, task_id: UUID) -> None: ...

    async def mark_failed(self, task_id: UUID, error_type: str) -> None: ...

    async def mark_cancelled(self, task_id: UUID) -> None: ...


class TaskCoordinator:
    """管理一次 LangGraph 任务的生命周期和稳定 checkpoint thread。"""

    def __init__(
            self,
            graph: Any | None,
            store: TaskStore,
            broker: EventBroker,
            langfuse: Langfuse | None = None,
            task_persistence: TaskPersistence | None = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._broker = broker
        self._langfuse = langfuse
        self._task_persistence = task_persistence
        self._running: dict[UUID, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        task_id: UUID,
        query: str,
        conversation_id: UUID | None = None,
    ) -> bool:
        """启动一次任务；同一 task_id 已运行时保持幂等并返回 False。"""

        async with self._lock:
            if task_id in self._running:
                return False
            existing = await self._store.get(task_id)
            if existing is not None and existing.status in {
                TaskStatus.COMPLETED,
                TaskStatus.CANCELLED,
            }:
                return False
            await self._prepare_persistent_task(task_id, query, conversation_id)
            task = asyncio.create_task(self.run(task_id, query, conversation_id))
            self._running[task_id] = task
            return True

    async def resume(
        self,
        task_id: UUID,
        query: str,
        conversation_id: UUID | None = None,
    ) -> bool:
        """使用同一个 thread_id 从 LangGraph 最新 checkpoint 继续。"""

        async with self._lock:
            if task_id in self._running:
                return False
            await self._prepare_persistent_task(task_id, query, conversation_id)
            task = asyncio.create_task(
                self.run(task_id, query, conversation_id, resume=True)
            )
            self._running[task_id] = task
            return True

    async def run(
            self,
            task_id: UUID,
            query: str,
            conversation_id: UUID | None = None,
            *,
            resume: bool = False,
    ) -> None:
        if self._langfuse is None:
            await self._run_task(
                task_id,
                query,
                conversation_id,
                resume=resume,
            )
            return

        with propagate_attributes(
                session_id=str(conversation_id) if conversation_id else None,
                metadata={
                    "task_id": str(task_id),
                    "resume": resume,
                    "feature": "evidence-research",
                },
                tags=["feature:evidence-research", "workflow:research-task"],
                trace_name="research-task",
        ):
            with self._langfuse.start_as_current_observation(
                    name="research-task",
                    as_type="agent",
                    input={
                        "query": query,
                        "resume": resume,
                    },
            ) as task_observation:
                await self._run_task(
                    task_id,
                    query,
                    conversation_id,
                    resume=resume,
                    observation=task_observation,
                )

    async def _run_task(
            self,
            task_id: UUID,
            query: str,
            conversation_id: UUID | None = None,
            *,
            resume: bool = False,
            observation: Any | None = None,
    ) -> None:
        """执行或恢复图，并把快照转换成 API 生命周期事件。"""

        await self._broker.publish(run_started(task_id, query))
        await self._store.update(
            task_id,
            status=TaskStatus.RUNNING,
            stage=TaskStage.CREATED,
        )
        if self._graph is None:
            await self._fail(
                task_id,
                "graph_not_configured",
                "Agent 图尚未配置",
                False,
            )
            if observation is not None:
                observation.update(
                    level="ERROR",
                    status_message="Agent 图尚未配置",
                    output={
                        "status": "failed",
                        "error_code": "graph_not_configured",
                    },
                )
            self._running.pop(task_id, None)
            await self._broker.close(task_id)
            return

        state = self._initial_state(task_id, query, conversation_id)
        previous_stage = "created"
        config = {"configurable": {"thread_id": str(task_id)}}
        graph_input: Any = None if resume else state
        try:
            async for snapshot in self._graph.astream(
                graph_input,
                config=config,
                stream_mode="values",
            ):
                if not isinstance(snapshot, Mapping):
                    continue
                state = dict(snapshot)
                stage_name = str(state.get("task_stage", previous_stage))
                if stage_name == previous_stage:
                    continue
                await self._broker.publish(step_finished(task_id, previous_stage))
                await self._broker.publish(step_started(task_id, stage_name))
                previous_stage = stage_name
                stage = public_stage(stage_name)
                if stage is not None:
                    await self._store.update(
                        task_id,
                        status=TaskStatus.RUNNING,
                        stage=stage,
                    )

            result = build_public_result(state)
            await self._sync_persistent_status("mark_completed", task_id)
            await self._store.update(
                task_id,
                status=TaskStatus.COMPLETED,
                stage=TaskStage.COMPLETED,
                result=result.model_dump(mode="json"),
            )
            message_id = f"{task_id}:answer"
            await self._broker.publish(text_started(task_id, message_id))
            await self._broker.publish(text_content(task_id, result.answer, message_id))
            await self._broker.publish(text_ended(task_id, message_id))
            await self._broker.publish(run_finished(task_id, result))
            if observation is not None:
                observation.update(
                    output={
                        "status": "completed",
                        "answer": result.answer,
                        "recommendation_count": len(result.recommendations),
                        "coverage_gap_count": len(result.coverage_gaps),
                        "search_overview": result.search_overview,
                    }
                )
        except asyncio.CancelledError:
            await self._sync_persistent_status("mark_cancelled", task_id)
            await self._store.update(
                task_id,
                status=TaskStatus.CANCELLED,
                stage=TaskStage.CANCELLED,
            )
            await self._broker.publish(run_cancelled(task_id))

            if observation is not None:
                observation.update(
                    level="WARNING",
                    status_message="研究任务已取消",
                    output={"status": "cancelled"},
                )

            raise
        except Exception:
            await self._fail(
                task_id,
                "task_execution_failed",
                "研究任务执行失败，请稍后重试",
                True,
            )
            if observation is not None:
                observation.update(
                    level="ERROR",
                    status_message="研究任务执行失败",
                    output={
                        "status": "failed",
                        "error_code": "task_execution_failed",
                    },
                )
        finally:
            self._running.pop(task_id, None)
            await self._broker.close(task_id)

    async def cancel(self, task_id: UUID) -> bool:
        """取消本进程中运行的任务。"""

        task = self._running.get(task_id)
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    @staticmethod
    def _initial_state(
        task_id: UUID,
        query: str,
        conversation_id: UUID | None,
    ) -> dict[str, Any]:
        return {
            "task_id": str(task_id),
            "task_stage": "created",
            "user_query": query,
            "query_clarification": False,
            "conversation_id": str(conversation_id) if conversation_id else None,
            "messages": [],
            "intent_analysis": {},
            "mesh_normalization": {},
            "search_plan": {},
            "search_source_decision": {},
            "search_sources": [],
            "search_branch_results": [],
            "search_results": [],
            "search_metrics": {},
            "article_groups": {},
            "quality_groups": [],
            "quality_evaluation_results": [],
            "quality_evaluation_failures": [],
            "evaluated_evidence": [],
            "verification_results": [],
            "verified_assessments": [],
            "excluded_candidates": [],
            "retained_candidates": [],
            "portfolio_evaluation": None,
            "ranked_candidates": [],
            "final_summary": None,
        }

    async def _fail(
        self,
        task_id: UUID,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        error = TaskError(code=code, message=message, retryable=retryable)
        await self._sync_persistent_status("mark_failed", task_id, code)
        await self._store.update(
            task_id,
            status=TaskStatus.FAILED,
            stage=TaskStage.FAILED,
            error=error,
        )
        await self._broker.publish(run_error(task_id, error))

    async def _prepare_persistent_task(
        self,
        task_id: UUID,
        query: str,
        conversation_id: UUID | None,
    ) -> None:
        if self._task_persistence is None:
            return
        try:
            await self._task_persistence.ensure_task(
                task_id=task_id,
                query=query,
                conversation_id=conversation_id,
            )
            await self._task_persistence.mark_running(task_id)
        except Exception as exc:
            logger.bind(
                component="task_coordinator",
                event="task_persistence_unavailable",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            ).exception("研究任务无法持久化，拒绝启动图")
            raise TaskPersistenceUnavailable("研究任务持久化不可用") from exc

    async def _sync_persistent_status(
        self,
        method_name: str,
        task_id: UUID,
        *args: str,
    ) -> None:
        """终态镜像失败只写日志，不能覆盖已经确定的 API 结果。"""

        if self._task_persistence is None:
            return
        try:
            method = getattr(self._task_persistence, method_name)
            await method(task_id, *args)
        except Exception as exc:
            logger.bind(
                component="task_coordinator",
                event="task_status_persistence_failed",
                task_id=str(task_id),
                operation=method_name,
                error_type=type(exc).__name__,
            ).exception("研究任务状态同步失败")
