""" Agent 使用的PostgreSQL仓库。"""

from backend.app.db.repositories.quality_evaluation_results import (
    QualityEvaluationBatchRepository,
)
from backend.app.db.repositories.search_results import SearchResultsRepository
from backend.app.db.repositories.tasks import (
    ResearchTaskRepository,
    TaskPersistenceUnavailable,
)

__all__ = [
    "QualityEvaluationBatchRepository",
    "ResearchTaskRepository",
    "SearchResultsRepository",
    "TaskPersistenceUnavailable",
]
