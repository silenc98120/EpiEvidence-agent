"""数据库快照和 dry-run 的公开数据合同。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .enums import BatchEligibility


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSnapshot(StrictModel):
    task_id: UUID
    user_query: str
    status: str
    created_at: datetime | None = None
    completed_at: datetime | None = None


class ArticleSnapshot(StrictModel):
    article_id: UUID
    title: str
    abstract: str | None = None
    abstract_available: bool | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    authors: list[dict[str, Any]] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    journal_abbreviation: str | None = None
    publication_date: date | None = None
    publication_year: int | None = None
    publication_types: list[str] = Field(default_factory=list)
    study_design: str | None = None
    publication_status: str | None = None
    language: str | None = None
    is_retracted: bool = False


class BatchSnapshot(StrictModel):
    batch_id: str
    group_id: str
    study_type: str
    status: str
    article_ids: list[UUID]
    input_payload: dict[str, Any] | None = None
    assessment_payload: dict[str, Any] | None = None
    score_payload: list[dict[str, Any]] | None = None
    error_type: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class TaskEvaluationSnapshot(StrictModel):
    task: TaskSnapshot
    batches: list[BatchSnapshot]
    articles: dict[UUID, ArticleSnapshot]


class CheckIssue(StrictModel):
    code: str
    message: str


class BatchCheckResult(StrictModel):
    batch_id: str
    classification: BatchEligibility
    article_count: int
    issues: list[CheckIssue] = Field(default_factory=list)
    rubric_contract_version: str | None = None
    rubric_sha256: str | None = None


class DryRunReport(StrictModel):
    task_id: UUID
    task_status: str
    batch_counts: dict[str, int]
    eligible_article_count: int
    study_type_distribution: dict[str, int]
    planned_calls: dict[str, int]
    estimated_llm_calls: int
    max_llm_calls: int | None
    within_call_budget: bool
    judge_provider: str | None
    judge_model: str | None
    prompt_versions: dict[str, str]
    batch_results: list[BatchCheckResult]


# ---------------------------------------------------------------------------
# Judge LLM 输出 Schema（压缩版：LLM 只输出 verdict + reason）
# ---------------------------------------------------------------------------

class ArticleRoutingVerdict(StrictModel):
    """Routing Judge 对单篇文章的研究类型判定。"""

    article_id: UUID
    verdict: str  # JudgeVerdict value
    reason: str = Field(min_length=1)


class RoutingJudgeOutput(StrictModel):
    """Routing Judge 的 LLM 直接输出。"""

    article_verdicts: list[ArticleRoutingVerdict] = Field(min_length=1)


class CriterionItemVerdict(StrictModel):
    """Criterion Judge 对单条 criterion 的审查判定。"""

    criterion_id: str = Field(min_length=1)
    verdict: str  # JudgeVerdict value
    reason: str = Field(min_length=1)
    expected_status: str | None = None  # 仅 disagree 时必填，CriterionStatus value


class ArticleCriterionVerdicts(StrictModel):
    """Criterion Judge 对单篇文章所有 criteria 的审查结果。"""

    article_id: UUID
    verdicts: list[CriterionItemVerdict] = Field(min_length=1)


class CriterionJudgeOutput(StrictModel):
    """Criterion Judge 的 LLM 直接输出。"""

    article_criterion_verdicts: list[ArticleCriterionVerdicts] = Field(min_length=1)


class RankedTier(StrictModel):
    """Ranking Judge 输出的一个质量层级（允许并列）。"""

    tier: int = Field(ge=1)
    article_ids: list[UUID] = Field(min_length=1)
    reason: str = Field(min_length=1)


class RankingJudgeOutput(StrictModel):
    """Ranking Judge 的 LLM 直接输出。"""

    judge_ranking: list[RankedTier] = Field(min_length=1)
    reason: str = Field(min_length=1)

