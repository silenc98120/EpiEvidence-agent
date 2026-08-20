"""HTTP routes for task lifecycle and AG-UI SSE events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    AGUIEventType,
    HealthResponse,
    TaskAcceptedResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskResponse,
)
from app.api.services.agui_events import run_error, run_finished, state_snapshot
from app.api.services.event_broker import EventBroker
from app.api.services.task_runner import TaskRunner
from app.api.services.task_store import TaskStore


def create_router(store: TaskStore, runner: TaskRunner, broker: EventBroker) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @router.post("/api/tasks", response_model=TaskAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_task(request: TaskCreateRequest) -> TaskAcceptedResponse:
        task = await store.create(request)
        await runner.start(task.task_id, task.query, task.conversation_id)
        return TaskAcceptedResponse(
            task_id=task.task_id,
            events_url=f"/api/tasks/{task.task_id}/events",
        )

    @router.get("/api/tasks", response_model=TaskListResponse)
    async def list_tasks() -> TaskListResponse:
        return TaskListResponse(items=await store.list())

    @router.get("/api/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(task_id: UUID) -> TaskResponse:
        task = await store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task

    @router.post("/api/tasks/{task_id}/cancel", response_model=TaskResponse)
    async def cancel_task(task_id: UUID) -> TaskResponse:
        task = await store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        await runner.cancel(task_id)
        return await store.get(task_id) or task

    @router.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: UUID, request: Request) -> StreamingResponse:
        if await store.get(task_id) is None:
            raise HTTPException(status_code=404, detail="任务不存在")

        async def stream() -> AsyncIterator[str]:
            task = await store.get(task_id)
            if task is None:
                return
            initial_event = state_snapshot(task)
            yield f"event: {initial_event.type.value}\ndata: {json.dumps(initial_event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            if task.status.value == "completed" and task.result is not None:
                from app.api.schemas import PublicResearchResult

                terminal = run_finished(task_id, PublicResearchResult.model_validate(task.result))
                yield f"event: {terminal.type.value}\ndata: {json.dumps(terminal.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                return
            if task.status.value == "failed" and task.error is not None:
                terminal = run_error(task_id, task.error)
                yield f"event: {terminal.type.value}\ndata: {json.dumps(terminal.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                return
            async for event in broker.subscribe(task_id):
                if await request.is_disconnected():
                    return
                yield f"event: {event.type.value}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router
