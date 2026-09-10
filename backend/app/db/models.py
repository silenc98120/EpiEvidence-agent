"""SQLAlchemy mappings for the PostgreSQL evidence-storage schema.

The baseline DDL in ``database/schema.sql`` remains the schema authority. These
models mirror that schema for application reads, writes, and relationships.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    Boolean,
    CHAR,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    and_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


UTC_TIMESTAMP = text("CURRENT_TIMESTAMP")


class TaskORM(Base):
    """后端根据用户查询自动生成的一条研究任务。"""

    __tablename__ = "tasks"

    task_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    client_request_id: Mapped[str | None] = mapped_column(Text)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="created", server_default=text("'created'")
    )
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    final_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    search_runs: Mapped[list[SearchRunORM]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    quality_evaluation_batches: Mapped[list[QualityEvaluationBatchORM]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(btrim(user_query)) > 0",
            name="ck_tasks_user_query_not_blank",
        ),
        CheckConstraint(
            "status IN ('created', 'recognizing_intent', 'searching', "
            "'screening', 'awaiting_user_selection', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_tasks_completion_time",
        ),
        UniqueConstraint("client_request_id", name="uq_tasks_client_request_id"),
        Index("ix_tasks_conversation_created", "conversation_id", "created_at"),
        Index("ix_tasks_status", "status"),
    )


class SearchRunORM(Base):
    """研究任务中针对特定提供商的一次搜索执行。"""

    __tablename__ = "search_runs"

    search_run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_query: Mapped[str] = mapped_column(Text, nullable=False)
    query_hash: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default=text("'pending'")
    )
    hit_count: Mapped[int | None] = mapped_column(Integer)
    retrieved_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )

    task: Mapped[TaskORM] = relationship(back_populates="search_runs")
    source_records: Mapped[list[SourceRecordORM]] = relationship(
        back_populates="search_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("length(btrim(source)) > 0", name="ck_search_runs_source_not_blank"),
        CheckConstraint(
            "length(btrim(compiled_query)) > 0",
            name="ck_search_runs_query_not_blank",
        ),
        CheckConstraint(
            "query_hash IS NULL OR query_hash ~ '^[0-9a-f]{64}$'",
            name="ck_search_runs_query_hash",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success_with_results', "
            "'success_empty', 'partial_success', 'failed')",
            name="ck_search_runs_status",
        ),
        CheckConstraint(
            "(hit_count IS NULL OR hit_count >= 0) AND retrieved_count >= 0 "
            "AND page_count >= 0 AND retry_count >= 0",
            name="ck_search_runs_counts",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="ck_search_runs_completion_time",
        ),
        Index("ix_search_runs_task_created", "task_id", "created_at"),
        Index("ix_search_runs_source_status", "source", "status"),
    )


class ArticleORM(Base):
    """搜索结果条目的统一规范化标准表示。"""

    __tablename__ = "articles"

    article_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    doi: Mapped[str | None] = mapped_column(Text)
    pmid: Mapped[str | None] = mapped_column(Text)
    pmcid: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    abstract_available: Mapped[bool | None] = mapped_column(
        Boolean,
        Computed(
            "abstract IS NOT NULL AND length(btrim(abstract)) > 0",
            persisted=True,
        ),
        nullable=True,
    )
    first_author: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::JSONB")
    )
    journal_title: Mapped[str | None] = mapped_column(Text)
    journal_abbreviation: Mapped[str | None] = mapped_column(Text)
    issn: Mapped[str | None] = mapped_column(Text)
    electronic_issn: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[date | None] = mapped_column(Date)
    publication_year: Mapped[int | None] = mapped_column(SmallInteger)
    publication_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("ARRAY[]::TEXT[]")
    )
    study_design: Mapped[str | None] = mapped_column(Text)
    publication_status: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    is_retracted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )

    source_records: Mapped[list[SourceRecordORM]] = relationship(
        back_populates="article",
        foreign_keys="SourceRecordORM.article_id",
    )
    full_text_resources: Mapped[list[FullTextResourceORM]] = relationship(
        back_populates="article",
        foreign_keys="FullTextResourceORM.article_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="ck_articles_title_not_blank"),
        CheckConstraint(
            "length(btrim(normalized_title)) > 0",
            name="ck_articles_normalized_title_not_blank",
        ),
        CheckConstraint(
            "doi IS NULL OR length(btrim(doi)) > 0", name="ck_articles_doi_not_blank"
        ),
        CheckConstraint("pmid IS NULL OR pmid ~ '^[0-9]+$'", name="ck_articles_pmid"),
        CheckConstraint(
            "pmcid IS NULL OR pmcid ~* '^PMC[0-9]+$'", name="ck_articles_pmcid"
        ),
        CheckConstraint(
            "jsonb_typeof(authors) = 'array'", name="ck_articles_authors_array"
        ),
        CheckConstraint(
            "publication_year IS NULL OR publication_year BETWEEN 1500 AND 3000",
            name="ck_articles_publication_year",
        ),
        Index("ix_articles_normalized_title", "normalized_title"),
        Index(
            "ix_articles_publication_date",
            text("publication_date DESC NULLS LAST"),
        ),
        Index("ix_articles_publication_types", "publication_types", postgresql_using="gin"),
        Index(
            "uq_articles_doi_normalized",
            text("(lower(btrim(doi)))"),
            unique=True,
            postgresql_where=text("doi IS NOT NULL"),
        ),
        Index(
            "uq_articles_pmid",
            "pmid",
            unique=True,
            postgresql_where=text("pmid IS NOT NULL"),
        ),
        Index(
            "uq_articles_pmcid_normalized",
            text("(upper(btrim(pmcid)))"),
            unique=True,
            postgresql_where=text("pmcid IS NOT NULL"),
        ),
    )


class SourceRecordORM(Base):
    """搜索结果中保留的原始提供商记录，用于数据溯源与回放重演。"""

    __tablename__ = "source_records"

    source_record_pk: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    search_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("search_runs.search_run_id", ondelete="CASCADE"),
        nullable=False,
    )
    article_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("articles.article_id", ondelete="SET NULL")
    )
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_rank: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::JSONB")
    )
    payload_hash: Mapped[str | None] = mapped_column(CHAR(64))
    normalization_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    normalization_error: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )
    normalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    search_run: Mapped[SearchRunORM] = relationship(back_populates="source_records")
    article: Mapped[ArticleORM | None] = relationship(
        back_populates="source_records",
        foreign_keys=[article_id],
    )
    full_text_resources: Mapped[list[FullTextResourceORM]] = relationship(
        back_populates="source_record",
        primaryjoin=lambda: and_(
            SourceRecordORM.source_record_pk == FullTextResourceORM.source_record_pk,
            SourceRecordORM.article_id == FullTextResourceORM.article_id,
        ),
        foreign_keys=lambda: [
            FullTextResourceORM.source_record_pk,
            FullTextResourceORM.article_id,
        ],
        viewonly=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "search_run_id", "source_record_id", name="uq_source_records_per_run"
        ),
        UniqueConstraint(
            "source_record_pk", "article_id", name="uq_source_records_article_pair"
        ),
        CheckConstraint(
            "length(btrim(source_record_id)) > 0",
            name="ck_source_records_id_not_blank",
        ),
        CheckConstraint(
            "source_rank IS NULL OR source_rank >= 1", name="ck_source_records_rank"
        ),
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object'",
            name="ck_source_records_payload_object",
        ),
        CheckConstraint(
            "payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_source_records_payload_hash",
        ),
        CheckConstraint(
            "normalization_status IN ('pending', 'success', 'failed')",
            name="ck_source_records_normalization_status",
        ),
        CheckConstraint(
            "normalized_at IS NULL OR normalized_at >= retrieved_at",
            name="ck_source_records_normalized_at",
        ),
        Index("ix_source_records_search_rank", "search_run_id", "source_rank"),
        Index(
            "ix_source_records_article",
            "article_id",
            postgresql_where=text("article_id IS NOT NULL"),
        ),
        Index("ix_source_records_normalization_status", "normalization_status"),
    )
class QualityEvaluationBatchORM(Base):
    """一次固定研究类型质量评估批次的可恢复结果快照。"""

    __tablename__ = "quality_evaluation_batches"

    quality_evaluation_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
    )
    task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    study_type: Mapped[str] = mapped_column(Text, nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    article_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
    )
    assessment_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verification_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    score_payload: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    retained_article_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::UUID[]"),
    )
    excluded_article_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default=text("ARRAY[]::UUID[]"),
    )
    error_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=UTC_TIMESTAMP,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=UTC_TIMESTAMP,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[TaskORM] = relationship(back_populates="quality_evaluation_batches")

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "batch_id",
            name="uq_quality_evaluation_batches_task_batch",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_quality_evaluation_batches_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_quality_evaluation_batches_attempt_count",
        ),
        CheckConstraint(
            "length(btrim(batch_id)) > 0",
            name="ck_quality_evaluation_batches_batch_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(group_id)) > 0",
            name="ck_quality_evaluation_batches_group_id_not_blank",
        ),
        CheckConstraint(
            "length(btrim(study_type)) > 0",
            name="ck_quality_evaluation_batches_study_type_not_blank",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_quality_evaluation_batches_completion_time",
        ),
        Index(
            "ix_quality_evaluation_batches_task_status",
            "task_id",
            "status",
        ),
    )

class FullTextResourceORM(Base):
    """一篇论文已检索到的 HTTP 全文资源记录。"""

    __tablename__ = "full_text_resources"

    resource_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    article_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("articles.article_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_record_pk: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    resource_url: Mapped[str] = mapped_column(Text, nullable=False)
    landing_url: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    original_style: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[str | None] = mapped_column(Text)
    availability_code: Mapped[str | None] = mapped_column(Text)
    is_open_access: Mapped[bool | None] = mapped_column(Boolean)
    is_downloadable: Mapped[bool | None] = mapped_column(Boolean)
    is_preferred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("FALSE")
    )
    suggested_file_name: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=UTC_TIMESTAMP
    )

    article: Mapped[ArticleORM] = relationship(
        back_populates="full_text_resources",
        foreign_keys=[article_id],
    )
    source_record: Mapped[SourceRecordORM] = relationship(
        back_populates="full_text_resources",
        primaryjoin=lambda: and_(
            SourceRecordORM.source_record_pk == FullTextResourceORM.source_record_pk,
            SourceRecordORM.article_id == FullTextResourceORM.article_id,
        ),
        foreign_keys=lambda: [
            FullTextResourceORM.source_record_pk,
            FullTextResourceORM.article_id,
        ],
        viewonly=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_record_pk", "article_id"],
            ["source_records.source_record_pk", "source_records.article_id"],
            name="fk_full_text_resources_source_article",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "article_id", "resource_url", name="uq_full_text_resources_article_url"
        ),
        CheckConstraint(
            "resource_url ~* '^https?://'", name="ck_full_text_resources_url"
        ),
        CheckConstraint(
            "landing_url IS NULL OR landing_url ~* '^https?://'",
            name="ck_full_text_resources_landing_url",
        ),
        CheckConstraint(
            "format IN ('pdf', 'epub', 'html', 'xml', 'text', 'other')",
            name="ck_full_text_resources_format",
        ),
        CheckConstraint(
            "NOT is_preferred OR is_downloadable IS TRUE",
            name="ck_full_text_resources_preferred_downloadable",
        ),
        Index("ix_full_text_resources_article", "article_id"),
        Index(
            "ix_full_text_resources_downloadable",
            "article_id",
            "format",
            postgresql_where=text("is_downloadable IS TRUE"),
        ),
        Index(
            "uq_full_text_resources_preferred_article",
            "article_id",
            unique=True,
            postgresql_where=text("is_preferred IS TRUE"),
        ),
    )
