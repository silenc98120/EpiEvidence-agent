"""文章 embedding 的 PostgreSQL 持久缓存。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.quality_candidate_selector import ArticleEmbeddingKey
from backend.app.db.models import ArticleEmbeddingORM


class ArticleEmbeddingRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_many(
        self, keys: Sequence[ArticleEmbeddingKey]
    ) -> dict[ArticleEmbeddingKey, list[float]]:
        if not keys:
            return {}
        identities = [
            (
                UUID(key.article_id),
                key.model_name,
                key.model_revision,
                key.representation_version,
                key.content_hash,
            )
            for key in keys
        ]
        key_by_identity = dict(zip(identities, keys, strict=True))
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ArticleEmbeddingORM).where(
                        tuple_(
                            ArticleEmbeddingORM.article_id,
                            ArticleEmbeddingORM.model_name,
                            ArticleEmbeddingORM.model_revision,
                            ArticleEmbeddingORM.representation_version,
                            ArticleEmbeddingORM.content_hash,
                        ).in_(identities)
                    )
                )
            ).all()
        return {
            key_by_identity[
                (
                    row.article_id,
                    row.model_name,
                    row.model_revision,
                    row.representation_version,
                    row.content_hash,
                )
            ]: list(row.embedding)
            for row in rows
        }

    async def put_many(
        self, values: Mapping[ArticleEmbeddingKey, Sequence[float]]
    ) -> None:
        rows = []
        for key, vector in values.items():
            normalized_vector = [float(item) for item in vector]
            if not normalized_vector:
                raise ValueError("article embedding 不能为空")
            rows.append(
                {
                    "article_embedding_id": uuid4(),
                    "article_id": UUID(key.article_id),
                    "model_name": key.model_name,
                    "model_revision": key.model_revision,
                    "representation_version": key.representation_version,
                    "content_hash": key.content_hash,
                    "dimension": len(normalized_vector),
                    "normalized": True,
                    "embedding": normalized_vector,
                }
            )
        if not rows:
            return
        statement = insert(ArticleEmbeddingORM).values(rows)
        statement = statement.on_conflict_do_nothing(
            constraint="uq_article_embeddings_identity"
        )
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(statement)
