"""质量评估批次结果的 PostgreSQL 持久化边界。"""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from backend.app.db.models import QualityEvaluationBatchORM

class QualityEvaluationBatchRepository:
    """管理质量评估批次的状态与结构化结果。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def register_pending(
        self,
        *,
        task_id: str,
        batch_id: str,
        group_id: str,
        study_type: str,
        article_ids: list[UUID],
    ) -> None:
        """在投递 Celery 前登记 pending 批次，供轮询器立即观察。"""
        task_uuid = UUID(task_id)
        statement = (
            insert(QualityEvaluationBatchORM)
            .values(
                quality_evaluation_batch_id=uuid4(),
                task_id=task_uuid,
                batch_id=batch_id,
                group_id=group_id,
                study_type=study_type,
                status="pending",
                attempt_count=0,
                article_ids=article_ids,
            )
            .on_conflict_do_nothing(
                constraint="uq_quality_evaluation_batches_task_batch"
            )
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def attach_celery_task_id(
        self,
        *,
        task_id: str,
        batch_id: str,
        celery_task_id: str,
    ) -> None:
        """把 Celery 回执写回 pending 批次；Worker 已领取时保持 processing。"""
        task_uuid = UUID(task_id)
        statement = (
            update(QualityEvaluationBatchORM)
            .where(
                QualityEvaluationBatchORM.task_id == task_uuid,
                QualityEvaluationBatchORM.batch_id == batch_id,
                QualityEvaluationBatchORM.status == "pending",
            )
            .values(celery_task_id=celery_task_id, updated_at=func.now())
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)

    async def claim_for_processing(
        self,
        *,
        task_id: str,
        batch_id: str,
        group_id: str,
        study_type: str,
        article_ids: list[UUID],
        celery_task_id: str | None,
    ) -> bool:
        """原子领取一个待处理或失败的质量评估批次。"""

        try:
            task_uuid = UUID(task_id)
        except ValueError as exc:
            raise ValueError("质量评估任务的 task_id 必须是 PostgreSQL UUID") from exc

        statement = (
            insert(QualityEvaluationBatchORM)
            .values(
                quality_evaluation_batch_id=uuid4(),
                task_id=task_uuid,
                batch_id=batch_id,
                group_id=group_id,
                study_type=study_type,
                celery_task_id=celery_task_id,
                status="processing",
                attempt_count=1,
                article_ids=article_ids,
            )
            .on_conflict_do_update(
                constraint="uq_quality_evaluation_batches_task_batch",
                set_={
                    "celery_task_id": celery_task_id,
                    "status": "processing",
                    "attempt_count": (
                        QualityEvaluationBatchORM.attempt_count + 1
                    ),
                    "error_type": None,
                    "error_message": None,
                    "updated_at": func.now(),
                },
                where=QualityEvaluationBatchORM.status.in_(
                    ("pending", "failed")
                ),
            )
            .returning(
                QualityEvaluationBatchORM.quality_evaluation_batch_id
            )
        )

        async with self._session_factory() as session:
            async with session.begin():
                claimed_batch_id = await session.scalar(statement)

        return claimed_batch_id is not None

    async def save_completed(
        self,
        *,
        task_id: str,
        batch_id: str,
        evaluation: Mapping[str, Any],
    ) -> None:
        """保存已通过结构化校验的质量评估结果。"""

        try:
            task_uuid = UUID(task_id)
            retained_article_ids = [
                UUID(str(article_id))
                for article_id in evaluation["retained_article_ids"]
            ]
            excluded_article_ids = [
                UUID(str(article_id))
                for article_id in evaluation["excluded_article_ids"]
            ]
            assessment_payload = dict(evaluation["assessment"])
            score_payload = [
                dict(score)
                for score in evaluation["scores"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("质量评估完成结果结构不合法") from exc

        statement = (
            update(QualityEvaluationBatchORM)
            .where(
                QualityEvaluationBatchORM.task_id == task_uuid,
                QualityEvaluationBatchORM.batch_id == batch_id,
                QualityEvaluationBatchORM.status == "processing",
            )
            .values(
                status="completed",
                assessment_payload=assessment_payload,
                score_payload=score_payload,
                retained_article_ids=retained_article_ids,
                excluded_article_ids=excluded_article_ids,
                error_type=None,
                error_message=None,
                updated_at=func.now(),
                completed_at=func.now(),
            )
            .returning(
                QualityEvaluationBatchORM.quality_evaluation_batch_id
            )
        )

        async with self._session_factory() as session:
            async with session.begin():
                updated_batch_id = await session.scalar(statement)

        if updated_batch_id is None:
            raise RuntimeError(
                "质量评估批次未处于 processing 状态，不能保存完成结果"
            )

    async def save_failed(
        self,
        *,
        task_id: str,
        batch_id: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """将已领取但未完成的质量评估批次标记为失败。"""

        try:
            task_uuid = UUID(task_id)
        except ValueError as exc:
            raise ValueError("质量评估任务的 task_id 必须是 PostgreSQL UUID") from exc

        normalized_error_type = error_type.strip()
        normalized_error_message = error_message.strip()[:2000]

        if not normalized_error_type:
            raise ValueError("error_type 不能为空")
        if not normalized_error_message:
            normalized_error_message = normalized_error_type

        statement = (
            update(QualityEvaluationBatchORM)
            .where(
                QualityEvaluationBatchORM.task_id == task_uuid,
                QualityEvaluationBatchORM.batch_id == batch_id,
                QualityEvaluationBatchORM.status == "processing",
            )
            .values(
                status="failed",
                error_type=normalized_error_type,
                error_message=normalized_error_message,
                updated_at=func.now(),
            )
            .returning(
                QualityEvaluationBatchORM.quality_evaluation_batch_id
            )
        )

        async with self._session_factory() as session:
            async with session.begin():
                updated_batch_id = await session.scalar(statement)

        if updated_batch_id is None:
            raise RuntimeError(
                "质量评估批次未处于 processing 状态，不能保存失败结果"
            )
