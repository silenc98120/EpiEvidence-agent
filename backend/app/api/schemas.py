"""本地 API 的公共 HTTP 和 SSE 数据契约。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TaskCreateRequest(BaseModel):
    """创建一次研究任务的请求。

    Attributes:
        query: 自然语言研究问题。
        conversation_id: 可选的对话 ID，用于关联历史对话。
    """

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
    """进程健康检查端点返回的响应。

    Attributes:
        status: 服务状态，固定为 "ok"。
        service: 服务名称，固定为 "epi-research-agent"。
    """

    status: Literal["ok"] = "ok"
    service: str = "epi-research-agent"


class TaskStatus(StrEnum):
    """HTTP API 暴露的稳定生命周期状态。

    Members:
        CREATED: 任务已创建。
        RUNNING: 任务正在运行。
        AWAITING_USER_INPUT: 任务正在等待用户输入。
        COMPLETED: 任务已完成。
        FAILED: 任务已失败。
        CANCELLED: 任务已取消。
    """

    CREATED = "created"
    RUNNING = "running"
    AWAITING_USER_INPUT = "awaiting_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(StrEnum):
    """从内部 Agent 节点映射的稳定用户可见阶段。

    Members:
        CREATED: 任务已创建。
        RECOGNIZING_INTENT: 正在识别用户意图。
        NORMALIZING_TERMS: 正在规范术语。
        BUILDING_SEARCH_PLAN: 正在构建搜索计划。
        SEARCHING: 正在执行搜索。
        MERGING_RESULTS: 正在合并搜索结果。
        EVALUATING_EVIDENCE: 正在评估证据。
        SUMMARIZING: 正在生成总结。
        AWAITING_USER_INPUT: 等待用户输入。
        COMPLETED: 任务已完成。
        FAILED: 任务已失败。
        CANCELLED: 任务已取消。
    """

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
    """前端可安全显示的错误信息。

    Attributes:
        code: 错误代码。
        message: 错误描述信息。
        retryable: 是否可重试。
        stage: 错误发生时的任务阶段（可选）。
    """

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    stage: TaskStage | None = None


class TaskAcceptedResponse(BaseModel):
    """后端接受新任务后返回的响应。

    Attributes:
        task_id: 任务唯一标识符。
        status: 任务初始状态，默认为 "created"。
        events_url: 事件流 SSE URL。
    """

    task_id: UUID
    status: TaskStatus = TaskStatus.CREATED
    events_url: str


class TaskResponse(BaseModel):
    """单个研究任务的当前公共快照。

    Attributes:
        task_id: 任务唯一标识符。
        conversation_id: 关联的对话 ID（可选）。
        query: 研究问题。
        status: 任务状态。
        stage: 当前任务阶段（可选）。
        result: 任务结果数据（可选）。
        error: 错误信息（可选）。
        created_at: 任务创建时间。
        updated_at: 任务最后更新时间。
    """

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
    """任务历史端点的响应封装。

    Attributes:
        items: 任务列表。
    """

    items: list[TaskResponse] = Field(default_factory=list)


class TaskEventType(StrEnum):
    """Runner、Broker 和 SSE 路由共享的事件名称。

    Members:
        SNAPSHOT: 状态快照事件。
        STAGE: 阶段变更事件。
        RETRYING: 重试事件。
        AWAITING_USER_INPUT: 等待用户输入事件。
        COMPLETED: 任务完成事件。
        FAILED: 任务失败事件。
        CANCELLED: 任务取消事件。
    """

    SNAPSHOT = "snapshot"
    STAGE = "stage"
    RETRYING = "retrying"
    AWAITING_USER_INPUT = "awaiting_user_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(BaseModel):
    """通过任务的 SSE 流传递的一个事件。

    Attributes:
        event: 事件类型。
        task_id: 任务唯一标识符。
        stage: 当前任务阶段（可选）。
        error: 错误信息（可选）。
        payload: 事件负载数据。
        created_at: 事件创建时间（UTC）。
    """

    event: TaskEventType
    task_id: UUID
    stage: TaskStage | None = None
    error: TaskError | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PublicResearchResult(BaseModel):
    """内部 Agent 状态完成后暴露的验证后结果。

    Attributes:
        task_id: 任务唯一标识符。
        query: 研究问题。
        answer: 研究答案。
        recommendations: 推荐建议列表。
        coverage_gaps: 研究覆盖缺口列表。
        conflict_summary: 证据冲突总结（可选）。
        no_recommendation_reason: 无推荐建议的原因（可选）。
        search_overview: 搜索概览元数据。
    """

    task_id: UUID
    query: str
    answer: str
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    conflict_summary: str | None = None
    no_recommendation_reason: str | None = None
    search_overview: dict[str, Any] = Field(default_factory=dict)


class AGUIEventType(StrEnum):
    """通过 SSE 发出的传输无关的 AG-UI 事件标签。

    Members:
        RUN_STARTED: 运行开始。
        STATE_SNAPSHOT: 状态快照。
        STEP_STARTED: 步骤开始。
        STEP_FINISHED: 步骤结束。
        TEXT_MESSAGE_START: 文本消息开始。
        TEXT_MESSAGE_CONTENT: 文本消息内容。
        TEXT_MESSAGE_END: 文本消息结束。
        RUN_ERROR: 运行错误。
        RUN_FINISHED: 运行结束。
    """

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
    """一个兼容 AG-UI 协议的事件，携带应用特定的数据。

    Attributes:
        type: 事件类型。
        task_id: 任务唯一标识符。
        name: 事件名称（可选）。
        message_id: 消息 ID，用于关联流式消息片段（可选）。
        delta: 增量文本内容（可选）。
        value: 事件负载数据。
        created_at: 事件创建时间（UTC）。
    """

    type: AGUIEventType
    task_id: UUID
    name: str | None = None
    message_id: str | None = None
    delta: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))