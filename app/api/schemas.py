"""Public HTTP and SSE data contracts for the local API."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TaskCreateRequest(BaseModel):
    """创建一次研究任务的请求。"""

    query: str = Field(min_length=1, description="自然语言研究问题")
    conversation_id: UUID | None = None

    @field_validator("query", mode="before")
    @classmethod
    def strip_query(cls, value: Any) -> Any:
        """在 Pydantic 进行数据验证前去除 query 中的空格。"""

        if isinstance(value, str):
            return value.strip()
        return value


class HealthResponse(BaseModel):
    """Response returned by the process health endpoint."""

    status: Literal["ok"] = "ok"
    service: str = "epi-research-agent"


class TaskStatus(StrEnum):
    """Stable lifecycle states exposed by the HTTP API."""

    CREATED = "created"
    RUNNING = "running"
    AWAITING_USER_INPUT = "awaiting_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(StrEnum):
    """Stable user-facing stages mapped from internal Agent nodes."""

    CREATED = "created"
    RECOGNIZING_INTENT = "recognizing_intent"
    NORMALIZING_TERMS = "normalizing_terms"
    BUILDING_SEARCH_PLAN = "building_search_plan"
    SEARCHING = "searching"
    MERGING_RESULTS = "merging_results"
    EVALUATING_EVIDENCE = "evaluating_evidence"
    SUMMARIZING = "summarizing"
    AWAITING_USER_INPUT = "awaiting_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskError(BaseModel):
    """Safe error information intended for display by the frontend."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    stage: TaskStage | None = None


class TaskAcceptedResponse(BaseModel):
    """Response returned after the backend accepts a new task."""

    task_id: UUID
    status: TaskStatus = TaskStatus.CREATED
    events_url: str


class TaskResponse(BaseModel):
    """Current public snapshot of one research task."""

    task_id: UUID
    conversation_id: UUID | None = None
    query: str
    status: TaskStatus
    stage: TaskStage | None = None
    result: dict[str, Any] | None = None
    error: TaskError | None = None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Envelope for the task history endpoint."""

    items: list[TaskResponse] = Field(default_factory=list)


class TaskEventType(StrEnum):
    """Event names shared by the runner, broker, and SSE route."""

    SNAPSHOT = "snapshot"
    STAGE = "stage"
    RETRYING = "retrying"
    AWAITING_USER_INPUT = "awaiting_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(BaseModel):
    """One event delivered through the task's SSE stream."""

    event: TaskEventType
    task_id: UUID
    stage: TaskStage | None = None
    error: TaskError | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PublicResearchResult(BaseModel):
    """Validated result exposed after the internal Agent state is complete."""

    task_id: UUID
    query: str
    answer: str
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    conflict_summary: str | None = None
    no_recommendation_reason: str | None = None
    search_overview: dict[str, Any] = Field(default_factory=dict)


class AGUIEventType(StrEnum):
    """Transport-neutral AG-UI event labels emitted through SSE."""

    RUN_STARTED = "RUN_STARTED"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    RUN_ERROR = "RUN_ERROR"
    RUN_FINISHED = "RUN_FINISHED"


class AGUIEvent(BaseModel):
    """One AG-UI-compatible event with application-specific value data."""

    type: AGUIEventType
    task_id: UUID
    name: str | None = None
    message_id: str | None = None
    delta: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
