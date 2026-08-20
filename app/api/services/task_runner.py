"""Run LangGraph tasks and translate updates into public events."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.api.schemas import (
    AGUIEvent,
    TaskError,
    TaskStage,
    TaskStatus,
)
from app.api.services.agui_events import (
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
from app.api.services.event_broker import EventBroker
from app.api.services.output_adapter import build_public_result
from app.api.services.task_store import TaskStore


class TaskRunner:
    """Own one background LangGraph execution for each accepted task."""

    def __init__(self, graph: Any | None, store: TaskStore, broker: EventBroker) -> None:
        self._graph = graph
        self._store = store
        self._broker = broker
        self._running: dict[UUID, asyncio.Task[None]] = {}

    async def start(self, task_id: UUID, query: str, conversation_id: UUID | None = None) -> None:
        """Schedule a task and return immediately to the HTTP handler."""

        task = asyncio.create_task(self.run(task_id, query, conversation_id))
        self._running[task_id] = task

    async def run(self, task_id: UUID, query: str, conversation_id: UUID | None = None) -> None:
        """Execute the graph, persist snapshots, and publish lifecycle events."""

        await self._broker.publish(run_started(task_id, query))
        await self._store.update(task_id, status=TaskStatus.RUNNING, stage=TaskStage.CREATED)
        if self._graph is None:
            await self._fail(task_id, "graph_not_configured", "Agent 图尚未配置", retryable=False)
            return

        state: dict[str, Any] = {
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
        previous_stage = "created"
        try:
            async for snapshot in self._graph.astream(state, stream_mode="values"):
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
                    await self._store.update(task_id, status=TaskStatus.RUNNING, stage=stage)

            result = build_public_result(state)
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
        except asyncio.CancelledError:
            await self._store.update(task_id, status=TaskStatus.CANCELLED, stage=TaskStage.CANCELLED)
            await self._broker.publish(run_cancelled(task_id))
            raise
        except Exception:
            await self._fail(task_id, "task_execution_failed", "研究任务执行失败，请稍后重试", retryable=True)
        finally:
            self._running.pop(task_id, None)
            await self._broker.close(task_id)

    async def cancel(self, task_id: UUID) -> bool:
        """Cancel a running local task if one exists."""

        task = self._running.get(task_id)
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _fail(self, task_id: UUID, code: str, message: str, retryable: bool) -> None:
        error = TaskError(code=code, message=message, retryable=retryable)
        await self._store.update(task_id, status=TaskStatus.FAILED, stage=TaskStage.FAILED, error=error)
        await self._broker.publish(run_error(task_id, error))
