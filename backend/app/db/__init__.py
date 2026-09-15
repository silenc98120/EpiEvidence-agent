"""Database access primitives for EpiEvidence."""

from backend.app.db.base import Base
from backend.app.db.repositories import (
    ResearchTaskRepository,
    SearchResultsRepository,
    ArticleEmbeddingRepository,
    TaskPersistenceUnavailable,
)
from backend.app.db.models import (
    ArticleORM,
    ArticleEmbeddingORM,
    FullTextResourceORM,
    QualityEvaluationBatchORM,
    SearchRunORM,
    SourceRecordORM,
    TaskORM,
    ConversationORM,
    ConversationMessageORM,
)
from backend.app.db.session import (
    build_async_engine,
    build_session_factory,
    get_database_url,
    ManagedSessionFactory,
    normalize_database_url,
    session_scope,
)

__all__ = [
    "ArticleORM",
    "ArticleEmbeddingORM",
    "Base",
    "FullTextResourceORM",
    "SearchRunORM",
    "SourceRecordORM",
    "TaskORM",
    "ConversationORM",
    "ConversationMessageORM",
    "SearchResultsRepository",
    "ArticleEmbeddingRepository",
    "ResearchTaskRepository",
    "TaskPersistenceUnavailable",
    "QualityEvaluationBatchORM",
    "build_async_engine",
    "build_session_factory",
    "get_database_url",
    "ManagedSessionFactory",
    "normalize_database_url",
    "session_scope",
]
