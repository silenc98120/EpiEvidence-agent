"""HTTP routes for task lifecycle and AG-UI SSE events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from backend.app.api.schemas import (
    AGUIEventType,
    HealthResponse,
    TaskAcceptedResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskResponse,
    TaskStatus,
    ConversationCreateRequest, ConversationRenameRequest, ConversationListResponse, ConversationResponse,
)
from backend.app.api.services.agui_events import run_error, run_finished, state_snapshot
from backend.app.api.services.event_broker import EventBroker
from backend.app.api.services.task_coordinator import (
    TaskCoordinator,
    TaskPersistenceUnavailable,
)
from backend.app.api.services.task_store import TaskStore
from backend.app.api.services.conversation_store import ConversationStore


def create_router(
    store: TaskStore,
    coordinator: TaskCoordinator,
    broker: EventBroker,
    conversations = None,
) -> APIRouter:
    router = APIRouter()
    conversations = conversations or ConversationStore()

    @router.post("/api/conversations", response_model=ConversationResponse)
    async def create_conversation(request: ConversationCreateRequest):
        return await conversations.create(request.title)

    @router.get("/api/conversations", response_model=ConversationListResponse)
    async def list_conversations():
        return ConversationListResponse(items=await conversations.list())

    @router.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
    async def get_conversation(conversation_id: UUID):
        item = await conversations.get(conversation_id)
        if item is None: raise HTTPException(404, "会话不存在")
        return item

    @router.patch("/api/conversations/{conversation_id}", response_model=ConversationResponse)
    async def rename_conversation(conversation_id: UUID, request: ConversationRenameRequest):
        if not await conversations.rename(conversation_id, request.title):
            raise HTTPException(404, "会话不存在")
        return await conversations.get(conversation_id)

    @router.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: UUID):
        if not await conversations.delete(conversation_id):
            raise HTTPException(404, "会话不存在")

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @router.post("/api/tasks", response_model=TaskAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_task(request: TaskCreateRequest) -> TaskAcceptedResponse:
        task = await store.create(request)
        try:
            await coordinator.start(task.task_id, task.query, task.conversation_id)
        except TaskPersistenceUnavailable as exc:
            await store.delete(task.task_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="研究任务存储暂不可用，请稍后重试",
            ) from exc
        if task.conversation_id:
            await conversations.append(task.conversation_id, "user", task.query, task.task_id)
            await conversations.update_status(task.conversation_id, "running", TaskStatus.RUNNING)
        return TaskAcceptedResponse(
            task_id=task.task_id,
            events_url=f"/api/tasks/{task.task_id}/events",
            conversation_id=task.conversation_id,
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
        await coordinator.cancel(task_id)
        return await store.get(task_id) or task

    @router.post("/api/tasks/{task_id}/resume", response_model=TaskResponse)
    async def resume_task(task_id: UUID) -> TaskResponse:
        task = await store.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        resumed = await coordinator.resume(task_id, task.query, task.conversation_id)
        if not resumed:
            return await store.get(task_id) or task
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
                from backend.app.api.schemas import PublicResearchResult

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
