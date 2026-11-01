"""质量任务的输入快照、原子领取和带所有权的结果持久化。"""
from __future__ import annotations
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from backend.app.db.models import QualityEvaluationBatchORM as Batch


class OwnershipLost(RuntimeError):
    """本次执行已经失去写入资格。"""


class QualityEvaluationBatchRepository:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def register_plan(self, task_id: str, execution_id: str, batches: list[dict]) -> None:
        if not batches:
            return
        values = [dict(quality_evaluation_batch_id=uuid4(), task_id=UUID(task_id),
            execution_id=UUID(execution_id), batch_id=b["batch_id"], group_id=b["group_id"],
            study_type=b["study_design"], article_ids=[UUID(a) for a in b["article_ids"]],
            input_payload=b["input_payload"], metrics_payload=b.get("metrics_payload"), status="pending", attempt_count=0) for b in batches]
        statement = insert(Batch).values(values).on_conflict_do_nothing(
            constraint="uq_quality_evaluation_batches_task_batch")
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)

    async def register_pending(self, *, task_id: str, batch_id: str, group_id: str,
                               study_type: str, article_ids: list[UUID]) -> None:
        statement = insert(Batch).values(quality_evaluation_batch_id=uuid4(), task_id=UUID(task_id),
            batch_id=batch_id, group_id=group_id, study_type=study_type,
            article_ids=article_ids, status="pending", attempt_count=0).on_conflict_do_nothing(
                constraint="uq_quality_evaluation_batches_task_batch")
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)

    async def read_batches(self, task_id: str, batch_ids: list[str]) -> list[dict]:
        if not batch_ids:
            return []
        async with self._session_factory() as session:
            rows = (await session.scalars(select(Batch).where(
                Batch.task_id == UUID(task_id), Batch.batch_id.in_(batch_ids)))).all()
            return [self._payload(row) for row in rows]

    async def claim_for_processing(self, *, task_id: str, batch_id: str, group_id: str,
                                   study_type: str, article_ids: list[UUID],
                                   lease_seconds: float = 120, max_attempts: int = 2) -> UUID | None:
        token = uuid4()
        statement = insert(Batch).values(quality_evaluation_batch_id=uuid4(), task_id=UUID(task_id),
            batch_id=batch_id, group_id=group_id, study_type=study_type, article_ids=article_ids,
            status="processing", attempt_id=token,
            lease_expires_at=func.now() + timedelta(seconds=lease_seconds), attempt_count=0,
        ).on_conflict_do_update(constraint="uq_quality_evaluation_batches_task_batch",
            set_=dict(status="processing", attempt_id=token,
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                error_type=None, error_message=None, completed_at=None, updated_at=func.now()),
            where=(Batch.attempt_count < max_attempts) & or_(
                Batch.status.in_(("pending", "failed")),
                (Batch.status == "processing") & (Batch.lease_expires_at <= func.now())),
        ).returning(Batch.attempt_id)
        async with self._session_factory() as session, session.begin():
            # 已耗尽请求预算且租约过期的中断执行，必须进入明确终态。
            await session.execute(update(Batch).where(Batch.task_id == UUID(task_id),
                Batch.batch_id == batch_id, Batch.status == "processing",
                Batch.lease_expires_at <= func.now(), Batch.attempt_count >= max_attempts
            ).values(status="failed", attempt_id=None, lease_expires_at=None,
                     error_type="attempts_exhausted", completed_at=func.now()))
            return await session.scalar(statement)

    def _owned(self, task_id: str, batch_id: str, attempt_id: UUID):
        return update(Batch).where(Batch.task_id == UUID(task_id), Batch.batch_id == batch_id,
            Batch.status == "processing", Batch.attempt_id == attempt_id,
            Batch.lease_expires_at > func.now())

    async def _write_owned(self, task_id: str, batch_id: str, attempt_id: UUID, **values):
        statement = self._owned(task_id, batch_id, attempt_id).values(
            **values, updated_at=func.now()).returning(Batch.batch_id)
        async with self._session_factory() as session, session.begin():
            if await session.scalar(statement) is None:
                raise OwnershipLost(f"批次 {batch_id} 已失去执行所有权")

    async def renew_lease(self, task_id: str, batch_id: str, attempt_id: UUID,
                          lease_seconds: float) -> None:
        await self._write_owned(task_id, batch_id, attempt_id,
                               lease_expires_at=func.now() + timedelta(seconds=lease_seconds))

    async def begin_attempt(self, task_id: str, batch_id: str, attempt_id: UUID,
                            metrics: dict | None = None) -> None:
        await self._write_owned(task_id, batch_id, attempt_id,
                               attempt_count=Batch.attempt_count + 1, metrics_payload=metrics)

    async def record_metrics(self, task_id: str, batch_id: str, attempt_id: UUID,
                             metrics: dict) -> None:
        await self._write_owned(task_id, batch_id, attempt_id, metrics_payload=metrics)

    async def save_completed(self, *, task_id: str, batch_id: str, attempt_id: UUID,
                             evaluation: dict, metrics: dict | None = None) -> None:
        await self._write_owned(task_id, batch_id, attempt_id, status="completed",
            assessment_payload=evaluation["assessment"], score_payload=evaluation["scores"],
            retained_article_ids=[UUID(a) for a in evaluation["retained_article_ids"]],
            excluded_article_ids=[UUID(a) for a in evaluation["excluded_article_ids"]],
            metrics_payload=metrics, error_type=None, error_message=None,
            completed_at=func.now(), lease_expires_at=None)

    async def save_failed(self, *, task_id: str, batch_id: str, attempt_id: UUID,
                          error_type: str, error_message: str,
                          metrics: dict | None = None, status: str = "failed") -> None:
        await self._write_owned(task_id, batch_id, attempt_id, status=status,
            error_type=error_type, error_message=error_message[:2000], metrics_payload=metrics,
            completed_at=func.now(), lease_expires_at=None)

    async def cancel_task(self, task_id: str) -> None:
        statement = update(Batch).where(Batch.task_id == UUID(task_id),
            Batch.status.in_(("pending", "processing"))).values(status="cancelled",
            attempt_id=None, lease_expires_at=None, completed_at=func.now())
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)

    async def resume_cancelled(self, task_id: str, max_attempts: int = 2) -> None:
        exhausted = Batch.attempt_count >= max_attempts
        statement = update(Batch).where(Batch.task_id == UUID(task_id),
            Batch.status == "cancelled").values(
                status=case((exhausted, "failed"), else_="pending"),
                error_type=case((exhausted, "attempts_exhausted"), else_=None),
                completed_at=case((exhausted, func.now()), else_=None))
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)

    async def latest_progress(self, task_id: str) -> dict | None:
        async with self._session_factory() as session:
            execution = await session.scalar(select(Batch.execution_id).where(
                Batch.task_id == UUID(task_id), Batch.execution_id.is_not(None)
            ).order_by(Batch.created_at.desc()).limit(1))
        if execution is None:
            return None
        return {"execution_id": str(execution), **await self.progress(task_id, str(execution))}

    async def progress(self, task_id: str, execution_id: str) -> dict:
        statement = select(Batch.status, func.count()).where(Batch.task_id == UUID(task_id),
            Batch.execution_id == UUID(execution_id), Batch.input_payload["kind"].astext == "article"
        ).group_by(Batch.status)
        async with self._session_factory() as session:
            counts = dict((await session.execute(statement)).all())
        return {"planned": sum(counts.values()), **{s: counts.get(s, 0)
            for s in ("pending", "processing", "completed", "failed", "cancelled")}}

    @staticmethod
    def _payload(row: Batch) -> dict:
        result = {c.name: getattr(row, c.name) for c in Batch.__table__.columns}
        for name in ("article_ids", "retained_article_ids", "excluded_article_ids"):
            result[name] = [str(a) for a in result[name]]
        return result
