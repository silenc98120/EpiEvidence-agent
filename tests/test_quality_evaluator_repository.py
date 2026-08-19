from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from skills.quality_evaluator.repository import ArticleRepository
from skills.quality_evaluator.schema import QualityEvaluationJob, StudyType


class FakeScalarResult:
    def __init__(self, articles: list[object]) -> None:
        self._articles = articles

    def all(self) -> list[object]:
        return self._articles


class FakeSession:
    def __init__(self, articles: list[object]) -> None:
        self.articles = articles
        self.statements: list[object] = []

    async def scalars(self, statement: object) -> FakeScalarResult:
        self.statements.append(statement)
        return FakeScalarResult(self.articles)


def _article(article_id, title: str) -> SimpleNamespace:
    return SimpleNamespace(
        article_id=article_id,
        title=title,
        abstract=f"{title} abstract",
        abstract_available=True,
        doi=None,
        pmid=None,
        pmcid=None,
        authors=[],
        first_author=None,
        journal_title=None,
        journal_abbreviation=None,
        publication_date=None,
        publication_year=2025,
        publication_types=[],
        study_design=StudyType.RCT.value,
        publication_status=None,
        language="en",
        is_retracted=False,
    )


def _repository(articles: list[object]) -> tuple[ArticleRepository, FakeSession]:
    session = FakeSession(articles)

    @asynccontextmanager
    async def session_factory():
        yield session

    return ArticleRepository(session_factory), session


@pytest.mark.anyio
async def test_repository_hydrates_canonical_uuids_in_requested_order() -> None:
    first_id = uuid4()
    second_id = uuid4()
    repository, session = _repository(
        [_article(second_id, "Second"), _article(first_id, "First")]
    )
    job = QualityEvaluationJob(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=[first_id, second_id],
    )

    batch = await repository.hydrate(job, user_query="HPV 疫苗是否有效？")

    assert [article.article_id for article in batch.articles] == [first_id, second_id]
    assert batch.articles[0].title == "First"
    assert len(session.statements) == 1


@pytest.mark.anyio
async def test_repository_rejects_missing_or_duplicate_database_rows() -> None:
    article_id = uuid4()
    job = QualityEvaluationJob(
        task_id="task-1",
        group_id="rct-1",
        study_type=StudyType.RCT,
        article_ids=[article_id],
    )

    missing_repository, _ = _repository([])
    with pytest.raises(LookupError, match="未找到"):
        await missing_repository.hydrate(job, user_query="query")

    duplicate_repository, _ = _repository(
        [_article(article_id, "First"), _article(article_id, "Duplicate")]
    )
    with pytest.raises(ValueError, match="重复"):
        await duplicate_repository.hydrate(job, user_query="query")
