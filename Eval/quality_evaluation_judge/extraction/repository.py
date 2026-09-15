"""Judge 专用 PostgreSQL 只读仓储。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.db.models import ArticleORM, QualityEvaluationBatchORM, TaskORM

from ..schemas import ArticleSnapshot, BatchSnapshot, TaskEvaluationSnapshot, TaskSnapshot


class TaskNotFoundError(LookupError):
    pass


class EvaluationReadRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_task(self, task_id: UUID) -> TaskEvaluationSnapshot:
        async with self._session_factory() as session:
            task = await session.scalar(select(TaskORM).where(TaskORM.task_id == task_id))
            if task is None:
                raise TaskNotFoundError(f"task_id 不存在：{task_id}")
            batches = list(
                (
                    await session.scalars(
                        select(QualityEvaluationBatchORM)
                        .where(QualityEvaluationBatchORM.task_id == task_id)
                        .order_by(QualityEvaluationBatchORM.created_at, QualityEvaluationBatchORM.batch_id)
                    )
                ).all()
            )
            article_ids = {article_id for batch in batches for article_id in batch.article_ids}
            articles = []
            if article_ids:
                articles = list(
                    (await session.scalars(select(ArticleORM).where(ArticleORM.article_id.in_(article_ids)))).all()
                )

        return TaskEvaluationSnapshot(
            task=TaskSnapshot.model_validate(task, from_attributes=True),
            batches=[BatchSnapshot.model_validate(item, from_attributes=True) for item in batches],
            articles={
                item.article_id: ArticleSnapshot.model_validate(item, from_attributes=True)
                for item in articles
            },
        )
