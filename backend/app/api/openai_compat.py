"""OpenAI-compatible endpoints for connecting Open WebUI."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.api.schemas import AGUIEventType, TaskCreateRequest
from backend.app.api.services.event_broker import EventBroker
from backend.app.api.services.task_store import TaskStore
from backend.app.runtime import TaskCoordinator


MODEL_ID = "epi-evidence-agent"


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


def _text(message: ChatMessage) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    if isinstance(message.content, list):
        return "".join(str(part.get("text", "")) for part in message.content).strip()
    return ""


def _chunk(request_id: str, content: str = "", *, finish: str | None = None) -> str:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_openai_router(store: TaskStore, coordinator: TaskCoordinator, broker: EventBroker) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/models")
    async def models() -> dict:
        return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "epi-research-agent"}]}

    async def events_for(request: ChatCompletionRequest) -> AsyncIterator[object]:
        query = next((_text(message) for message in reversed(request.messages) if message.role == "user" and _text(message)), "")
        if not query:
            raise HTTPException(status_code=422, detail="messages 中缺少非空 user 消息")
        task = await store.create(TaskCreateRequest(query=query))
        await coordinator.start(task.task_id, task.query, task.conversation_id)
        async for event in broker.subscribe(task.task_id):
            yield event

    @router.post("/chat/completions")
    async def chat_completions(request: ChatCompletionRequest, http_request: Request):
        if request.model != MODEL_ID:
            raise HTTPException(status_code=404, detail=f"未知模型: {request.model}")
        request_id = f"chatcmpl-{uuid4().hex}"

        async def stream() -> AsyncIterator[str]:
            sent_answer = False
            yield _chunk(request_id)
            async for event in events_for(request):
                if await http_request.is_disconnected():
                    return
                if event.type in {AGUIEventType.STEP_STARTED, AGUIEventType.STEP_FINISHED}:
                    stage = str(event.value.get("stage", event.name or "处理任务"))
                    yield _chunk(request_id, f"正在执行：{stage}……")
                elif event.type is AGUIEventType.TEXT_MESSAGE_CONTENT:
                    answer = event.delta or ""
                    if answer:
                        yield _chunk(request_id, answer)
                        sent_answer = True
                elif event.type is AGUIEventType.RUN_FINISHED:
                    answer = str(event.value.get("result", {}).get("answer", ""))
                    if answer and not sent_answer:
                        yield _chunk(request_id, answer)
                        sent_answer = True
                    yield _chunk(request_id, finish="stop")
                    yield "data: [DONE]\n\n"
                    return
                elif event.type is AGUIEventType.RUN_ERROR:
                    message = str(event.value.get("error", {}).get("message", "研究任务执行失败"))
                    yield _chunk(request_id, message)
                    yield _chunk(request_id, finish="stop")
                    yield "data: [DONE]\n\n"
                    return
                elif event.type is AGUIEventType.RUN_FINISHED and event.value.get("status") == "cancelled":
                    yield _chunk(request_id, "研究任务已取消")
                    yield _chunk(request_id, finish="stop")
                    yield "data: [DONE]\n\n"
                    return

        if request.stream:
            return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

        answer = ""
        async for event in events_for(request):
            if event.type is AGUIEventType.RUN_FINISHED:
                answer = str(event.value.get("result", {}).get("answer", ""))
                break
            if event.type is AGUIEventType.RUN_ERROR:
                raise HTTPException(status_code=502, detail=str(event.value.get("error", {}).get("message", "研究任务执行失败")))
        return {"id": request_id, "object": "chat.completion", "created": int(time.time()), "model": MODEL_ID, "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}]}

    return router
