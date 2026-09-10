"""Quality Evaluator 的 PostgreSQL 文章读取边界。"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from backend.app.db import ArticleORM, SearchRunORM, SourceRecordORM
from backend.skills.quality_evaluator.schema import HydratedArticleBatch, QualityEvaluationJob, EvaluationArticle


class ArticleRepository:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def hydrate(
        self,
        job: QualityEvaluationJob,
        *,
        user_query: str,
    ) -> HydratedArticleBatch:
        try:
            task_id = UUID(job.task_id)
        except ValueError as exc:
            raise ValueError("质量评估任务的 task_id 必须是 PostgreSQL UUID") from exc

        statement = (
            select(ArticleORM)
            .join(
                SourceRecordORM,
                SourceRecordORM.article_id == ArticleORM.article_id,
            )
            .join(
                SearchRunORM,
                SearchRunORM.search_run_id == SourceRecordORM.search_run_id,
            )
            .where(
                ArticleORM.article_id.in_(job.article_ids),
                SearchRunORM.task_id == task_id,
            )
            .distinct()
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())

        by_id: dict[Any, Any] = {}
        for row in rows:
            if row.article_id in by_id:
                raise ValueError(f"数据库返回重复 article_id: {row.article_id}")
            by_id[row.article_id] = row

        missing = [article_id for article_id in job.article_ids if article_id not in by_id]
        if missing:
            raise LookupError(f"PostgreSQL 未找到 article_id: {missing}")

        return HydratedArticleBatch(
            job=job,
            user_query=user_query,
            articles=[
                self._to_evaluation_article(by_id[article_id])
                for article_id in job.article_ids
            ],
        )

    @staticmethod
    def _to_evaluation_article(row: Any) -> EvaluationArticle:
        return EvaluationArticle(
            article_id=row.article_id,
            title=row.title,
            abstract=row.abstract,
            abstract_available=row.abstract_available,
            doi=row.doi,
            pmid=row.pmid,
            pmcid=row.pmcid,
            authors=row.authors or [],
            first_author=row.first_author,
            journal_title=row.journal_title,
            journal_abbreviation=row.journal_abbreviation,
            publication_date=row.publication_date,
            publication_year=row.publication_year,
            publication_types=row.publication_types or [],
            study_design=row.study_design,
            publication_status=row.publication_status,
            language=row.language,
            is_retracted=row.is_retracted,
        )
