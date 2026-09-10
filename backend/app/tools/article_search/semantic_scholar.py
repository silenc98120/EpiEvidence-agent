"""Semantic Scholar Academic Graph API 异步检索工具。"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import UTC, date, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import BaseModel, Field, model_validator


SEMANTIC_SCHOLAR_SEARCH_ENDPOINT = (
    "https://api.semanticscholar.org/graph/v1/paper/search"
)
SEMANTIC_SCHOLAR_BULK_SEARCH_ENDPOINT = (
    "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
)
SEMANTIC_SCHOLAR_FIELDS = ",".join(
    (
        "paperId",
        "externalIds",
        "url",
        "title",
        "abstract",
        "venue",
        "publicationVenue",
        "year",
        "publicationDate",
        "publicationTypes",
        "journal",
        "authors",
        "citationCount",
        "influentialCitationCount",
        "isOpenAccess",
        "openAccessPdf",
        "fieldsOfStudy",
    )
)


class SemanticScholarSortMode(StrEnum):
    """Semantic Scholar 检索排序方式。"""

    RELEVANCE = "relevance"
    NEWEST = "newest"


class SemanticScholarSearchStatus(StrEnum):
    """一次 Semantic Scholar 检索的结束状态。"""

    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class SemanticScholarSearchRequest(BaseModel):
    """Semantic Scholar 检索请求。"""

    query: str = Field(min_length=1)
    max_results: int = Field(default=100, ge=1, le=100)
    sort_mode: SemanticScholarSortMode = SemanticScholarSortMode.RELEVANCE
    publication_date_start: date | None = None
    publication_date_end: date | None = None
    open_access_only: bool = False
    api_key: str | None = None
    search_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @model_validator(mode="after")
    def normalize_and_validate(self) -> "SemanticScholarSearchRequest":
        self.query = " ".join(self.query.strip().split())
        if not self.query:
            raise ValueError("Semantic Scholar 检索词不能为空")
        self.api_key = self.api_key.strip() or None if self.api_key else None
        if (
            self.publication_date_start
            and self.publication_date_end
            and self.publication_date_start > self.publication_date_end
        ):
            raise ValueError("publication_date_start 不能晚于 publication_date_end")
        return self


class SemanticScholarAuthor(BaseModel):
    """Semantic Scholar 作者信息。"""

    author_id: str | None = None
    name: str


class SemanticScholarFullTextResource(BaseModel):
    """Semantic Scholar 报告的开放 PDF。"""

    resource_id: str
    url: str
    format: str = "pdf"
    status: str | None = None
    license: str | None = None
    is_open_access: bool = True
    is_downloadable: bool = True
    suggested_file_name: str


class SemanticScholarArticle(BaseModel):
    """供统一化和摘要筛选使用的 Semantic Scholar 记录。"""

    source: str = "semantic_scholar"
    source_record_id: str
    article_id: str
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    corpus_id: str | None = None
    arxiv_id: str | None = None
    title: str
    abstract: str | None = None
    abstract_available: bool = False
    authors: list[SemanticScholarAuthor] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    publication_types: list[str] = Field(default_factory=list)
    fields_of_study: list[str] = Field(default_factory=list)
    citation_count: int | None = None
    influential_citation_count: int | None = None
    is_open_access: bool | None = None
    has_full_text: bool = False
    full_text_resources: list[SemanticScholarFullTextResource] = Field(
        default_factory=list
    )
    landing_url: str | None = None
    retrieved_at: datetime
    search_run_id: str


class SemanticScholarSearchError(BaseModel):
    """不包含查询正文或 API Key 的错误摘要。"""

    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class SemanticScholarSearchResult(BaseModel):
    """一次 Semantic Scholar 检索结果。"""

    source: str = "semantic_scholar"
    search_run_id: str
    status: SemanticScholarSearchStatus
    hit_count: int | None = None
    retrieved_count: int = 0
    invalid_record_count: int = 0
    page_count: int = 0
    retry_count: int = 0
    records: list[SemanticScholarArticle] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    error: SemanticScholarSearchError | None = None


class _SemanticScholarFailure(Exception):
    def __init__(
        self,
        *,
        error: SemanticScholarSearchError,
        retry_count: int,
    ) -> None:
        super().__init__(error.message)
        self.error = error
        self.retry_count = retry_count


class SemanticScholarSearcher:
    """通过 Semantic Scholar Academic Graph API 检索文献。"""

    def __init__(
        self,
        *,
        search_endpoint: str = SEMANTIC_SCHOLAR_SEARCH_ENDPOINT,
        bulk_search_endpoint: str = SEMANTIC_SCHOLAR_BULK_SEARCH_ENDPOINT,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        if max_retries < 0:
            raise ValueError("max_retries 不能小于 0")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds 不能小于 0")
        self.search_endpoint = search_endpoint
        self.bulk_search_endpoint = bulk_search_endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def search(
        self,
        request: SemanticScholarSearchRequest,
    ) -> SemanticScholarSearchResult:
        start_time = perf_counter()
        if self._client is not None:
            return await self._search_with_client(request, self._client, start_time)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            return await self._search_with_client(request, client, start_time)

    async def _search_with_client(
        self,
        request: SemanticScholarSearchRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> SemanticScholarSearchResult:
        try:
            endpoint, params = self._build_request(request)
            headers = {"Accept": "application/json"}
            if request.api_key:
                headers["x-api-key"] = request.api_key
            response, retry_count = await self._request(
                client=client,
                endpoint=endpoint,
                params=params,
                headers=headers,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise _SemanticScholarFailure(
                    error=SemanticScholarSearchError(
                        error_type="invalid_json",
                        message="Semantic Scholar 未返回有效 JSON",
                    ),
                    retry_count=retry_count,
                ) from exc

            raw_records = payload.get("data", [])
            if not isinstance(raw_records, list):
                raise _SemanticScholarFailure(
                    error=SemanticScholarSearchError(
                        error_type="invalid_response_schema",
                        message="Semantic Scholar 响应缺少 data 数组",
                    ),
                    retry_count=retry_count,
                )

            records: list[SemanticScholarArticle] = []
            invalid_record_count = 0
            retrieved_at = datetime.now(UTC)
            for raw_record in raw_records[: request.max_results]:
                try:
                    records.append(
                        _normalize_article(
                            raw_record,
                            search_run_id=request.search_run_id,
                            retrieved_at=retrieved_at,
                        )
                    )
                except (TypeError, ValueError):
                    invalid_record_count += 1

            if invalid_record_count:
                status = SemanticScholarSearchStatus.PARTIAL_SUCCESS
            elif records:
                status = SemanticScholarSearchStatus.SUCCESS_WITH_RESULTS
            else:
                status = SemanticScholarSearchStatus.SUCCESS_EMPTY
            result = SemanticScholarSearchResult(
                search_run_id=request.search_run_id,
                status=status,
                hit_count=_optional_int(payload.get("total")),
                retrieved_count=len(records),
                invalid_record_count=invalid_record_count,
                page_count=1,
                retry_count=retry_count,
                records=records,
                latency_ms=(perf_counter() - start_time) * 1000,
            )
            self._log_result(result)
            return result
        except _SemanticScholarFailure as exc:
            result = SemanticScholarSearchResult(
                search_run_id=request.search_run_id,
                status=SemanticScholarSearchStatus.FAILED,
                retry_count=exc.retry_count,
                latency_ms=(perf_counter() - start_time) * 1000,
                error=exc.error,
            )
            self._log_result(result)
            return result
        except Exception as exc:
            logger.bind(
                component="semantic_scholar_searcher",
                event="semantic_scholar_search_unexpected_error",
                search_run_id=request.search_run_id,
                error_type=type(exc).__name__,
            ).exception("Semantic Scholar 检索发生未预期错误")
            result = SemanticScholarSearchResult(
                search_run_id=request.search_run_id,
                status=SemanticScholarSearchStatus.FAILED,
                latency_ms=(perf_counter() - start_time) * 1000,
                error=SemanticScholarSearchError(
                    error_type=type(exc).__name__,
                    message="Semantic Scholar 响应处理失败",
                ),
            )
            self._log_result(result)
            return result

    def _build_request(
        self,
        request: SemanticScholarSearchRequest,
    ) -> tuple[str, dict[str, str]]:
        params = {
            "query": request.query,
            "fields": SEMANTIC_SCHOLAR_FIELDS,
        }
        if request.sort_mode == SemanticScholarSortMode.NEWEST:
            endpoint = self.bulk_search_endpoint
            params["sort"] = "publicationDate:desc"
        else:
            endpoint = self.search_endpoint
            params["limit"] = str(request.max_results)
            params["offset"] = "0"
        if request.publication_date_start or request.publication_date_end:
            start = (
                request.publication_date_start.isoformat()
                if request.publication_date_start
                else ""
            )
            end = (
                request.publication_date_end.isoformat()
                if request.publication_date_end
                else ""
            )
            params["publicationDateOrYear"] = f"{start}:{end}"
        if request.open_access_only:
            params["openAccessPdf"] = ""
        return endpoint, params

    async def _request(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> tuple[httpx.Response, int]:
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        await self._wait_before_retry(attempt)
                        continue
                    raise _SemanticScholarFailure(
                        error=SemanticScholarSearchError(
                            error_type="http_error",
                            message=(
                                f"Semantic Scholar 返回 HTTP {response.status_code}"
                            ),
                            http_status=response.status_code,
                            retryable=True,
                        ),
                        retry_count=attempt,
                    )
                response.raise_for_status()
                return response, attempt
            except _SemanticScholarFailure:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _SemanticScholarFailure(
                    error=SemanticScholarSearchError(
                        error_type=type(exc).__name__,
                        message="Semantic Scholar 网络请求失败",
                        retryable=True,
                    ),
                    retry_count=attempt,
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise _SemanticScholarFailure(
                    error=SemanticScholarSearchError(
                        error_type="http_error",
                        message=(
                            f"Semantic Scholar 返回 HTTP {exc.response.status_code}"
                        ),
                        http_status=exc.response.status_code,
                    ),
                    retry_count=attempt,
                ) from exc
        raise RuntimeError("unreachable")

    async def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        jitter = random.uniform(0, delay * 0.25) if delay else 0
        await asyncio.sleep(delay + jitter)

    @staticmethod
    def _log_result(result: SemanticScholarSearchResult) -> None:
        logger.bind(
            component="semantic_scholar_searcher",
            event="semantic_scholar_search_completed",
            search_run_id=result.search_run_id,
            status=result.status.value,
            hit_count=result.hit_count,
            retrieved_count=result.retrieved_count,
            invalid_record_count=result.invalid_record_count,
            retry_count=result.retry_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("Semantic Scholar 检索完成")


def _normalize_article(
    raw: dict[str, Any],
    *,
    search_run_id: str,
    retrieved_at: datetime,
) -> SemanticScholarArticle:
    if not isinstance(raw, dict):
        raise TypeError("Semantic Scholar 记录必须是对象")
    paper_id = _clean_text(raw.get("paperId"))
    title = _clean_text(raw.get("title"))
    if not paper_id or not title:
        raise ValueError("Semantic Scholar 记录缺少 paperId 或 title")

    external_ids = raw.get("externalIds")
    if not isinstance(external_ids, dict):
        external_ids = {}
    authors = _normalize_authors(raw.get("authors"))
    abstract = _clean_text(raw.get("abstract"))
    resources = _normalize_full_text(raw.get("openAccessPdf"), paper_id)
    journal = raw.get("journal") if isinstance(raw.get("journal"), dict) else {}
    publication_venue = (
        raw.get("publicationVenue")
        if isinstance(raw.get("publicationVenue"), dict)
        else {}
    )
    publication_types = _string_list(raw.get("publicationTypes"))
    return SemanticScholarArticle(
        source_record_id=paper_id,
        article_id=f"semantic_scholar:paper:{paper_id}",
        doi=_external_id(external_ids, "DOI"),
        pmid=_external_id(external_ids, "PubMed"),
        pmcid=_external_id(external_ids, "PubMedCentral"),
        corpus_id=_external_id(external_ids, "CorpusId"),
        arxiv_id=_external_id(external_ids, "ArXiv"),
        title=title,
        abstract=abstract,
        abstract_available=abstract is not None,
        authors=authors,
        first_author=authors[0].name if authors else None,
        journal_title=(
            _clean_text(journal.get("name"))
            or _clean_text(publication_venue.get("name"))
            or _clean_text(raw.get("venue"))
        ),
        publication_date=_clean_text(raw.get("publicationDate")),
        publication_year=_optional_int(raw.get("year")),
        publication_types=publication_types,
        fields_of_study=_string_list(raw.get("fieldsOfStudy")),
        citation_count=_optional_int(raw.get("citationCount")),
        influential_citation_count=_optional_int(
            raw.get("influentialCitationCount")
        ),
        is_open_access=_optional_bool(raw.get("isOpenAccess")),
        has_full_text=bool(resources),
        full_text_resources=resources,
        landing_url=_http_url(raw.get("url")),
        retrieved_at=retrieved_at,
        search_run_id=search_run_id,
    )


def _normalize_authors(value: Any) -> list[SemanticScholarAuthor]:
    if not isinstance(value, list):
        return []
    result: list[SemanticScholarAuthor] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"))
        if name:
            result.append(
                SemanticScholarAuthor(
                    author_id=_clean_text(item.get("authorId")),
                    name=name,
                )
            )
    return result


def _normalize_full_text(
    value: Any,
    paper_id: str,
) -> list[SemanticScholarFullTextResource]:
    if not isinstance(value, dict):
        return []
    url = _http_url(value.get("url"))
    if not url:
        return []
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return [
        SemanticScholarFullTextResource(
            resource_id=f"semantic_scholar:{paper_id}:pdf:{digest}",
            url=url,
            status=_clean_text(value.get("status")),
            license=_clean_text(value.get("license")),
            suggested_file_name=f"semantic_scholar_{paper_id}_{digest}.pdf",
        )
    ]


def _external_id(external_ids: dict[str, Any], key: str) -> str | None:
    for candidate_key, value in external_ids.items():
        if str(candidate_key).casefold() == key.casefold():
            return _clean_text(value)
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_text(item)
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            result.append(cleaned)
    return result


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    return cleaned or None


def _http_url(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    return cleaned if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None
