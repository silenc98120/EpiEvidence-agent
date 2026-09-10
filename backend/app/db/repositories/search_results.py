"""PostgreSQL persistence for provider search results.

The repository deliberately owns database concerns only.  Search nodes provide
provider models and normalized ``EvidenceRecord`` values; this module stores
their provenance and returns canonical UUIDs for downstream graph nodes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from loguru import logger

from backend.app.core.search_results_normalizer import EvidenceRecord

from backend.app.db.models import ArticleORM, FullTextResourceORM, SearchRunORM, SourceRecordORM, TaskORM


class SearchResultsRepository:
    """Persist search provenance and canonical articles in one PostgreSQL boundary."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def persist_search_result(
        self,
        *,
        task_id: UUID,
        user_query: str,
        search_run_id: UUID,
        source: str,
        compiled_query: str,
        provider_result: Any,
        unified_result: Any,
    ) -> None:
        """Persist one completed provider invocation and all raw source records."""

        task_id = _as_uuid(task_id, "task_id")
        search_run_id = _as_uuid(search_run_id, "search_run_id")
        now = datetime.now(timezone.utc)
        raw_records = _provider_records(provider_result)
        normalized_by_source_id = {
            record.source_record_id: record
            for record in getattr(unified_result, "records", [])
        }
        async with self._session_factory() as session:
            async with session.begin():
                await self._ensure_task(session, task_id, user_query)
                search_run = SearchRunORM(
                    search_run_id=search_run_id,
                    task_id=task_id,
                    source=source,
                    compiled_query=compiled_query,
                    query_hash=_sha256(compiled_query),
                    status=str(unified_result.status.value),
                    hit_count=unified_result.hit_count,
                    retrieved_count=unified_result.retrieved_count,
                    page_count=unified_result.page_count,
                    retry_count=unified_result.retry_count,
                    started_at=now,
                    completed_at=now,
                )
                session.add(search_run)
                await session.flush()

                for rank, raw_record in enumerate(raw_records, start=1):
                    payload = _model_dump(raw_record)
                    source_record_id = _source_record_id(raw_record, payload)
                    normalized = normalized_by_source_id.get(source_record_id)
                    retrieved_at = normalized.retrieved_at if normalized else now
                    session.add(
                        SourceRecordORM(
                            source_record_pk=uuid4(),
                            search_run_id=search_run_id,
                            source_record_id=source_record_id,
                            source_rank=rank,
                            raw_payload=payload,
                            payload_hash=_payload_hash(payload),
                            normalization_status="success" if normalized else "failed",
                            normalization_error=None if normalized else "统一化失败或记录被丢弃",
                            retrieved_at=retrieved_at,
                            normalized_at=retrieved_at if normalized else None,
                        )
                    )
        logger.bind(
            component="search_results_repository",
            event="search_result_persisted",
            task_id=str(task_id),
            search_run_id=str(search_run_id),
            source=source,
            provider_record_count=len(raw_records),
            normalized_record_count=len(normalized_by_source_id),
        ).info("检索来源结果已持久化")

    async def persist_search_failure(
        self,
        *,
        task_id: UUID,
        user_query: str,
        search_run_id: UUID,
        source: str,
        compiled_query: str,
        error_type: str,
        error_message: str,
        retry_count: int = 0,
    ) -> None:
        """Persist a failed provider invocation without aborting other sources."""

        task_id = _as_uuid(task_id, "task_id")
        search_run_id = _as_uuid(search_run_id, "search_run_id")
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            async with session.begin():
                await self._ensure_task(session, task_id, user_query)
                session.add(
                    SearchRunORM(
                        search_run_id=search_run_id,
                        task_id=task_id,
                        source=source,
                        compiled_query=compiled_query,
                        query_hash=_sha256(compiled_query),
                        status="failed",
                        retrieved_count=0,
                        page_count=0,
                        retry_count=retry_count,
                        started_at=now,
                        completed_at=now,
                    )
                )
                await session.flush()
                # SearchRunORM currently has no error columns in the baseline schema.
                # Keep the failure observable through the status and structured log.

    async def persist_canonical_results(
        self,
        *,
        task_id: UUID,
        all_records: Sequence[EvidenceRecord],
        canonical_records: Sequence[EvidenceRecord],
    ) -> list[dict[str, Any]]:
        """Persist canonical articles and return records carrying canonical UUIDs."""

        task_id = _as_uuid(task_id, "task_id")
        if not all_records and not canonical_records:
            return []

        async with self._session_factory() as session:
            async with session.begin():
                await self._ensure_task(session, task_id, "检索任务")
                article_by_source_key: dict[tuple[str, str], ArticleORM] = {}
                source_row_by_key: dict[tuple[str, str], SourceRecordORM] = {}

                for record in all_records:
                    article = await self._get_or_create_article(session, record)
                    source_key = _source_key(record)
                    article_by_source_key[source_key] = article
                    source_row = await self._find_source_record(session, record)
                    if source_row is None:
                        raise LookupError(
                            "无法关联 source_record；请先持久化搜索分支结果: "
                            f"{record.search_run_id}/{record.source_record_id}"
                        )
                    source_row.article_id = article.article_id
                    source_row.normalization_status = "success"
                    source_row.normalized_at = record.retrieved_at
                    source_row_by_key[source_key] = source_row

                await session.flush()
                for record in all_records:
                    article = article_by_source_key[_source_key(record)]
                    source_row = source_row_by_key[_source_key(record)]
                    await self._upsert_resources(
                        session,
                        article_id=article.article_id,
                        source_record_pk=source_row.source_record_pk,
                        record=record,
                    )

                await session.flush()
                result: list[dict[str, Any]] = []
                for record in canonical_records:
                    article = article_by_source_key.get(_source_key(record))
                    if article is None:
                        article = await self._get_or_create_article(session, record)
                    result.append(
                        {
                            **record.model_dump(mode="json"),
                            "article_id": str(article.article_id),
                            "canonical_article_id": str(article.article_id),
                        }
                    )
                logger.bind(
                    component="search_results_repository",
                    event="canonical_articles_persisted",
                    task_id=str(task_id),
                    source_record_count=len(all_records),
                    canonical_article_count=len(result),
                ).info("规范文献结果已持久化")
                return result

    async def _ensure_task(
        self,
        session: AsyncSession,
        task_id: UUID,
        user_query: str,
    ) -> TaskORM:
        task = await session.get(TaskORM, task_id)
        if task is None:
            task = TaskORM(task_id=task_id, user_query=user_query.strip() or "检索任务")
            session.add(task)
            await session.flush()
        return task

    async def _get_or_create_article(
        self,
        session: AsyncSession,
        record: EvidenceRecord,
    ) -> ArticleORM:
        article = await self._find_article(session, record)
        if article is None:
            article = ArticleORM(
                article_id=uuid4(),
                doi=_clean(record.doi),
                pmid=_clean(record.pmid),
                pmcid=_clean(record.pmcid),
                title=record.title,
                normalized_title=record.normalized_title,
                abstract=record.abstract,
                first_author=record.first_author,
                authors=[item.model_dump(mode="json") for item in record.authors],
                journal_title=record.journal_title,
                journal_abbreviation=record.journal_abbreviation,
                issn=record.issn,
                electronic_issn=record.electronic_issn,
                publication_date=_parse_date(record.publication_date),
                publication_year=record.publication_year,
                publication_types=list(record.publication_types),
                study_design=record.study_design,
                publication_status=record.publication_status,
                language=record.language,
                is_retracted=record.is_retracted,
            )
            session.add(article)
            await session.flush()
            return article

        _fill_missing_article_fields(article, record)
        return article

    async def _find_article(
        self,
        session: AsyncSession,
        record: EvidenceRecord,
    ) -> ArticleORM | None:
        conditions = []
        if record.doi:
            conditions.append(func.lower(func.btrim(ArticleORM.doi)) == record.doi.strip().casefold())
        if record.pmid:
            conditions.append(ArticleORM.pmid == record.pmid.strip())
        if record.pmcid:
            conditions.append(func.upper(func.btrim(ArticleORM.pmcid)) == record.pmcid.strip().upper())
        if conditions:
            row = await session.scalar(select(ArticleORM).where(conditions[0]))
            if row is not None:
                return row
        if record.publication_year is not None and record.first_author:
            return await session.scalar(
                select(ArticleORM).where(
                    ArticleORM.normalized_title == record.normalized_title,
                    ArticleORM.publication_year == record.publication_year,
                    func.lower(func.btrim(ArticleORM.first_author))
                    == record.first_author.strip().casefold(),
                )
            )
        return None

    async def _find_source_record(
        self,
        session: AsyncSession,
        record: EvidenceRecord,
    ) -> SourceRecordORM | None:
        try:
            search_run_id = UUID(str(record.search_run_id))
        except ValueError as exc:
            raise ValueError(f"search_run_id 不是 UUID: {record.search_run_id}") from exc
        return await session.scalar(
            select(SourceRecordORM).where(
                SourceRecordORM.search_run_id == search_run_id,
                SourceRecordORM.source_record_id == record.source_record_id,
            )
        )

    async def _upsert_resources(
        self,
        session: AsyncSession,
        *,
        article_id: UUID,
        source_record_pk: UUID,
        record: EvidenceRecord,
    ) -> None:
        resources = list(record.full_text_resources)
        if not resources:
            return
        preferred = _preferred_resource(resources)
        for resource in resources:
            existing = await session.scalar(
                select(FullTextResourceORM).where(
                    FullTextResourceORM.article_id == article_id,
                    FullTextResourceORM.resource_url == resource.url,
                )
            )
            is_preferred = resource is preferred and resource.is_downloadable is True
            if existing is None:
                session.add(
                    FullTextResourceORM(
                        resource_id=uuid4(),
                        article_id=article_id,
                        source_record_pk=source_record_pk,
                        resource_url=resource.url,
                        landing_url=record.landing_url,
                        format=resource.format.value,
                        original_style=resource.original_style,
                        availability=resource.availability,
                        availability_code=resource.availability_code,
                        is_open_access=resource.is_open_access,
                        is_downloadable=resource.is_downloadable,
                        is_preferred=is_preferred,
                        suggested_file_name=resource.suggested_file_name,
                    )
                )
            else:
                existing.source_record_pk = source_record_pk
                existing.landing_url = existing.landing_url or record.landing_url
                existing.is_open_access = (
                    True if resource.is_open_access is True else existing.is_open_access
                )
                existing.is_downloadable = (
                    True if resource.is_downloadable is True else existing.is_downloadable
                )
                if is_preferred:
                    existing.is_preferred = True

        if preferred is not None:
            rows = list(
                (
                    await session.scalars(
                        select(FullTextResourceORM).where(
                            FullTextResourceORM.article_id == article_id
                        )
                    )
                ).all()
            )
            selected_url = preferred.url
            for row in rows:
                if row.resource_url != selected_url:
                    row.is_preferred = False


def _provider_records(provider_result: Any) -> list[Any]:
    records = getattr(provider_result, "records", [])
    return list(records) if isinstance(records, Sequence) else []


def _as_uuid(value: UUID | str, field_name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 不是有效 UUID: {value}") from exc


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": str(value)}


def _source_record_id(value: Any, payload: Mapping[str, Any]) -> str:
    source_record_id = getattr(value, "source_record_id", None) or payload.get("source_record_id")
    if source_record_id:
        return str(source_record_id)
    for key in ("pmid", "pmcid", "doi", "article_id", "id"):
        if payload.get(key):
            return str(payload[key])
    raise ValueError("provider record 缺少 source_record_id")


def _source_key(record: EvidenceRecord) -> tuple[str, str]:
    return (str(record.search_run_id), record.source_record_id)


def _fill_missing_article_fields(article: ArticleORM, record: EvidenceRecord) -> None:
    fields = {
        "doi": record.doi,
        "pmid": record.pmid,
        "pmcid": record.pmcid,
        "abstract": record.abstract,
        "first_author": record.first_author,
        "journal_title": record.journal_title,
        "journal_abbreviation": record.journal_abbreviation,
        "issn": record.issn,
        "electronic_issn": record.electronic_issn,
        "publication_date": _parse_date(record.publication_date),
        "publication_year": record.publication_year,
        "study_design": record.study_design,
        "publication_status": record.publication_status,
        "language": record.language,
    }
    for name, value in fields.items():
        if getattr(article, name) in (None, "") and value not in (None, ""):
            setattr(article, name, value)
    if not article.authors and record.authors:
        article.authors = [item.model_dump(mode="json") for item in record.authors]
    if not article.publication_types and record.publication_types:
        article.publication_types = list(record.publication_types)
    article.is_retracted = article.is_retracted or record.is_retracted


def _preferred_resource(resources: Sequence[Any]) -> Any | None:
    downloadable = [item for item in resources if item.is_downloadable is True]
    if not downloadable:
        return None
    priority = {"pdf": 0, "xml": 1, "html": 2, "epub": 3, "text": 4, "other": 5}
    return min(downloadable, key=lambda item: priority.get(item.format.value, 99))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _clean(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded)
