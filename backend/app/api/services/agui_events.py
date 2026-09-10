"""Map task lifecycle changes to AG-UI-compatible events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from backend.app.api.schemas import (
    AGUIEvent,
    AGUIEventType,
    PublicResearchResult,
    TaskError,
    TaskResponse,
    TaskStage,
)


_STAGE_MAP: dict[str, TaskStage] = {
    "created": TaskStage.CREATED,
    "intent_recognized": TaskStage.RECOGNIZING_INTENT,
    "mesh_normalized": TaskStage.NORMALIZING_TERMS,
    "search_plan_built": TaskStage.BUILDING_SEARCH_PLAN,
    "search_sources_selected": TaskStage.BUILDING_SEARCH_PLAN,
    "searching": TaskStage.SEARCHING,
    "search_results_merged": TaskStage.MERGING_RESULTS,
    "articles_grouped": TaskStage.EVALUATING_EVIDENCE,
    "quality_evaluated": TaskStage.EVALUATING_EVIDENCE,
    "quality_ranked": TaskStage.EVALUATING_EVIDENCE,
    "completed": TaskStage.COMPLETED,
    "summary_failed": TaskStage.FAILED,
}


def public_stage(raw_stage: str | None) -> TaskStage | None:
    """Map an internal graph stage to a stable frontend stage."""

    return _STAGE_MAP.get(raw_stage or "")


def run_started(task_id: UUID, query: str) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.RUN_STARTED,
        task_id=task_id,
        value={"query": query},
    )


def state_snapshot(task: TaskResponse) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.STATE_SNAPSHOT,
        task_id=task.task_id,
        value={"snapshot": task.model_dump(mode="json")},
    )


def step_started(task_id: UUID, raw_stage: str) -> AGUIEvent:
    stage = public_stage(raw_stage)
    return AGUIEvent(
        type=AGUIEventType.STEP_STARTED,
        task_id=task_id,
        name=stage.value if stage else raw_stage,
        value={"stage": stage.value if stage else raw_stage},
    )


def step_finished(task_id: UUID, raw_stage: str, payload: Mapping[str, Any] | None = None) -> AGUIEvent:
    stage = public_stage(raw_stage)
    return AGUIEvent(
        type=AGUIEventType.STEP_FINISHED,
        task_id=task_id,
        name=stage.value if stage else raw_stage,
        value={"stage": stage.value if stage else raw_stage, **dict(payload or {})},
    )


def text_started(task_id: UUID, message_id: str) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_START,
        task_id=task_id,
        message_id=message_id,
    )


def text_content(task_id: UUID, text: str, message_id: str) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_CONTENT,
        task_id=task_id,
        message_id=message_id,
        delta=text,
    )


def text_ended(task_id: UUID, message_id: str) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.TEXT_MESSAGE_END,
        task_id=task_id,
        message_id=message_id,
    )


def run_error(task_id: UUID, error: TaskError) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.RUN_ERROR,
        task_id=task_id,
        value={"error": error.model_dump(mode="json")},
    )


def run_finished(task_id: UUID, result: PublicResearchResult) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.RUN_FINISHED,
        task_id=task_id,
        value={"result": result.model_dump(mode="json")},
    )


def run_cancelled(task_id: UUID) -> AGUIEvent:
    return AGUIEvent(
        type=AGUIEventType.RUN_FINISHED,
        task_id=task_id,
        value={"status": "cancelled"},
    )
