"""质量评估 Celery 子 Agent 的固定路由与执行入口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from backend.app.core.llm_provider import create_chat_model
from backend.app.db.models import TaskORM
from backend.app.db.session import build_session_factory
from backend.app.workers.celery_app import celery_app
from backend.app.workers.celery_contracts import QualityBatchMessage
from backend.skills.quality_evaluator.evaluator import QualityEvaluatorRuntime
from backend.skills.quality_evaluator.repository import ArticleRepository
from backend.skills.quality_evaluator.schema import StudyType
from backend.skills.quality_evaluator.subgraph import (
    GroupEvaluationResult,
    QualityEvaluationPipeline,
)
from backend.app.db.repositories import QualityEvaluationBatchRepository

@dataclass(frozen=True)
class QualityTaskSpec:
    """一个固定研究类型质量评估子 Agent 的调度规格。"""

    task_name: str
    queue_name: str
    study_type: StudyType


QUALITY_TASK_SPECS = (
    QualityTaskSpec(
        task_name="quality.evaluate_rct_batch",
        queue_name="quality-rct",
        study_type=StudyType.RCT,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_cohort_batch",
        queue_name="quality-cohort",
        study_type=StudyType.COHORT,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_case_control_batch",
        queue_name="quality-case-control",
        study_type=StudyType.CASE_CONTROL,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_cross_sectional_analytical_batch",
        queue_name="quality-cross-sectional-analytical",
        study_type=StudyType.CROSS_SECTIONAL_ANALYTICAL,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_cross_sectional_prevalence_batch",
        queue_name="quality-cross-sectional-prevalence",
        study_type=StudyType.CROSS_SECTIONAL_PREVALENCE,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_systematic_review_batch",
        queue_name="quality-systematic-review",
        study_type=StudyType.SYSTEMATIC_REVIEW,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_meta_analysis_batch",
        queue_name="quality-meta-analysis",
        study_type=StudyType.META_ANALYSIS,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_qualitative_systematic_review_batch",
        queue_name="quality-qualitative-systematic-review",
        study_type=StudyType.QUALITATIVE_SYSTEMATIC_REVIEW,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_umbrella_review_batch",
        queue_name="quality-umbrella-review",
        study_type=StudyType.UMBRELLA_REVIEW,
    ),
    QualityTaskSpec(
        task_name="quality.evaluate_narrative_review_batch",
        queue_name="quality-narrative-review",
        study_type=StudyType.NARRATIVE_REVIEW,
    ),
)

QUALITY_TASK_ROUTES = {
    spec.task_name: {
        "queue": spec.queue_name,
        "routing_key": spec.queue_name,
    }
    for spec in QUALITY_TASK_SPECS
}
celery_app.conf.task_routes = QUALITY_TASK_ROUTES

QUALITY_TASK_SPEC_BY_NAME = {
    spec.task_name: spec
    for spec in QUALITY_TASK_SPECS
}

_session_factory: Any | None = None
_quality_pipeline: QualityEvaluationPipeline | None = None
_quality_evaluation_repository: QualityEvaluationBatchRepository | None = None

def _get_session_factory() -> Any:
    global _session_factory

    if _session_factory is None:
        _session_factory = build_session_factory()

    return _session_factory

def _get_quality_evaluation_repository() -> QualityEvaluationBatchRepository:
    """返回当前 Celery Worker 进程复用的质量评估结果 Repository。"""

    global _quality_evaluation_repository

    if _quality_evaluation_repository is None:
        _quality_evaluation_repository = QualityEvaluationBatchRepository(
            _get_session_factory()
        )

    return _quality_evaluation_repository

def _get_quality_pipeline() -> QualityEvaluationPipeline:
    global _quality_pipeline

    if _quality_pipeline is None:
        session_factory = _get_session_factory()
        article_repository = ArticleRepository(session_factory)

        quality_runtime = QualityEvaluatorRuntime(
            model=create_chat_model(),
            repository=article_repository,
        )

        _quality_pipeline = QualityEvaluationPipeline(
            runtime=quality_runtime,
        )

    return _quality_pipeline


async def _load_user_query(task_id: str) -> str:
    """从任务事实表读取本批评估所需的用户问题。"""

    try:
        task_uuid = UUID(task_id)
    except ValueError as exc:
        raise ValueError("质量评估任务的 task_id 必须是 PostgreSQL UUID") from exc

    session_factory = _get_session_factory()

    async with session_factory() as session:
        user_query = await session.scalar(
            select(TaskORM.user_query).where(
                TaskORM.task_id == task_uuid,
            )
        )

    if not user_query:
        raise LookupError(f"PostgreSQL 未找到 task_id={task_id} 的用户问题")

    return user_query


async def _process_fixed_study_batch(
    payload: dict[str, Any],
    *,
    spec: QualityTaskSpec,
    celery_task_id: str | None,
) -> tuple[QualityBatchMessage, GroupEvaluationResult | None]:
    """领取、执行并持久化一个固定研究类型的质量评估批次。"""

    message = QualityBatchMessage.model_validate(payload)
    job = message.job

    if job.study_type is not spec.study_type:
        raise ValueError(
            f"{spec.task_name} 只接受 {spec.study_type.value} 批次"
        )

    repository = _get_quality_evaluation_repository()
    claimed = await repository.claim_for_processing(
        task_id=job.task_id,
        batch_id=message.batch_id,
        group_id=job.group_id,
        study_type=spec.study_type.value,
        article_ids=job.article_ids,
        celery_task_id=celery_task_id,
    )

    if not claimed:
        logger.bind(
            component="quality_worker",
            worker_type=spec.study_type.value,
            task_id=job.task_id,
            batch_id=message.batch_id,
            celery_task_id=celery_task_id,
        ).info("质量评估批次已在执行或已完成，跳过重复处理")
        return message, None

    try:
        user_query = await _load_user_query(job.task_id)

        result = await _get_quality_pipeline().evaluate_branch(
            {
                "task_id": job.task_id,
                "user_query": user_query,
                "intent_analysis": {},
                "search_plan": {},
                "quality_group": {
                    "group_id": job.group_id,
                    "study_design": spec.study_type.value,
                    "article_ids": [
                        str(article_id)
                        for article_id in job.article_ids
                    ],
                },
            }
        )

        await repository.save_completed(
            task_id=job.task_id,
            batch_id=message.batch_id,
            evaluation=result.to_dict(),
        )
        return message, result

    except Exception as exc:
        try:
            await repository.save_failed(
                task_id=job.task_id,
                batch_id=message.batch_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception:
            logger.bind(
                component="quality_worker",
                task_id=job.task_id,
                batch_id=message.batch_id,
                original_error_type=type(exc).__name__,
            ).exception("质量评估失败状态无法写入 PostgreSQL")

        raise


def _execute_fixed_study_task(
    celery_task: Any,
    payload: dict[str, Any],
    *,
    spec: QualityTaskSpec,
) -> dict[str, Any]:
    """运行固定子 Agent，并返回轻量的执行回执。"""

    celery_task_id = (
        str(celery_task.request.id)
        if celery_task.request.id
        else None
    )
    message, result = asyncio.run(
        _process_fixed_study_batch(
            payload,
            spec=spec,
            celery_task_id=celery_task_id,
        )
    )

    if result is None:
        return {
            "status": "skipped",
            "reason": "batch_already_processing_or_completed",
            "task_id": message.job.task_id,
            "group_id": message.job.group_id,
            "batch_id": message.batch_id,
            "study_type": spec.study_type.value,
            "celery_task_id": celery_task_id,
        }

    logger.bind(
        component="quality_worker",
        worker_type=spec.study_type.value,
        task_id=message.job.task_id,
        group_id=message.job.group_id,
        batch_id=message.batch_id,
        celery_task_id=celery_task_id,
        article_count=len(message.job.article_ids),
        retained_count=len(result.retained_article_ids),
        excluded_count=len(result.excluded_article_ids),
    ).info("质量评估批次已完成并持久化")

    return {
        "status": "completed",
        "task_id": message.job.task_id,
        "group_id": message.job.group_id,
        "batch_id": message.batch_id,
        "study_type": spec.study_type.value,
        "celery_task_id": celery_task_id,
        "article_count": len(message.job.article_ids),
        "retained_count": len(result.retained_article_ids),
        "excluded_count": len(result.excluded_article_ids),
    }


@celery_app.task(
    bind=True,
    name="quality.evaluate_rct_batch",
)
def evaluate_rct_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """RCT 质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME["quality.evaluate_rct_batch"],
    )


@celery_app.task(bind=True, name="quality.evaluate_cohort_batch")
def evaluate_cohort_batch(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """队列研究质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME["quality.evaluate_cohort_batch"],
    )


@celery_app.task(bind=True, name="quality.evaluate_case_control_batch")
def evaluate_case_control_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """病例对照研究质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME["quality.evaluate_case_control_batch"],
    )


@celery_app.task(
    bind=True,
    name="quality.evaluate_cross_sectional_analytical_batch",
)
def evaluate_cross_sectional_analytical_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """分析型横断面研究质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME[
            "quality.evaluate_cross_sectional_analytical_batch"
        ],
    )


@celery_app.task(
    bind=True,
    name="quality.evaluate_cross_sectional_prevalence_batch",
)
def evaluate_cross_sectional_prevalence_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """患病率横断面研究质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME[
            "quality.evaluate_cross_sectional_prevalence_batch"
        ],
    )


@celery_app.task(bind=True, name="quality.evaluate_systematic_review_batch")
def evaluate_systematic_review_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """系统评价质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME[
            "quality.evaluate_systematic_review_batch"
        ],
    )


@celery_app.task(bind=True, name="quality.evaluate_meta_analysis_batch")
def evaluate_meta_analysis_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Meta 分析质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME["quality.evaluate_meta_analysis_batch"],
    )


@celery_app.task(
    bind=True,
    name="quality.evaluate_qualitative_systematic_review_batch",
)
def evaluate_qualitative_systematic_review_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """定性系统评价质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME[
            "quality.evaluate_qualitative_systematic_review_batch"
        ],
    )


@celery_app.task(bind=True, name="quality.evaluate_umbrella_review_batch")
def evaluate_umbrella_review_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """伞状评价质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME[
            "quality.evaluate_umbrella_review_batch"
        ],
    )


@celery_app.task(bind=True, name="quality.evaluate_narrative_review_batch")
def evaluate_narrative_review_batch(
    self: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """叙述性综述质量评估子 Agent 的 Celery 执行入口。"""

    return _execute_fixed_study_task(
        self,
        payload,
        spec=QUALITY_TASK_SPEC_BY_NAME["quality.evaluate_narrative_review_batch"],
    )
