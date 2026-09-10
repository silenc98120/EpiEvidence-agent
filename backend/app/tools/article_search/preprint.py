"""bioRxiv 和 medRxiv 官方 API 的 DOI 校验与元数据补充。"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import BaseModel, Field, field_validator


PREPRINT_DETAILS_ENDPOINT = "https://api.biorxiv.org/details"


class PreprintServer(StrEnum):
    BIORXIV = "biorxiv"
    MEDRXIV = "medrxiv"


class PreprintSearchStatus(StrEnum):
    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class PreprintLookupRequest(BaseModel):
    """由 Europe PMC 发现阶段提供的候选 DOI。"""

    dois: list[str] = Field(min_length=1, max_length=100)
    max_results: int = Field(default=100, ge=1, le=100)
    concurrency: int = Field(default=5, ge=1, le=10)
    search_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @field_validator("dois")
    @classmethod
    def normalize_dois(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if not cleaned.startswith("10.") or "/" not in cleaned:
                raise ValueError("预印本候选必须是 DOI")
            if cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                result.append(cleaned)
        if not result:
            raise ValueError("至少需要一个 DOI")
        return result


class PreprintArticle(BaseModel):
    source: str
    source_record_id: str
    article_id: str
    doi: str
    title: str
    abstract: str | None = None
    abstract_available: bool = False
    authors: list[str] = Field(default_factory=list)
    first_author: str | None = None
    corresponding_author: str | None = None
    corresponding_institution: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    version: int | None = None
    category: str | None = None
    publication_type: str | None = None
    license: str | None = None
    jatsxml_url: str | None = None
    published_doi: str | None = None
    preprint_server: PreprintServer
    peer_review_status: str = "preprint"
    has_full_text: bool = False
    landing_url: str
    retrieved_at: datetime
    search_run_id: str


class PreprintLookupError(BaseModel):
    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class PreprintSearchResult(BaseModel):
    source: str
    search_run_id: str
    status: PreprintSearchStatus
    requested_count: int
    retrieved_count: int = 0
    failed_lookup_count: int = 0
    retry_count: int = 0
    records: list[PreprintArticle] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    error: PreprintLookupError | None = None


class _PreprintFailure(Exception):
    def __init__(self, *, error: PreprintLookupError, retry_count: int) -> None:
        super().__init__(error.message)
        self.error = error
        self.retry_count = retry_count


class PreprintSearcher:
    """对一个预印本服务器执行有界并发 DOI 校验。"""

    def __init__(
        self,
        *,
        server: PreprintServer,
        endpoint: str = PREPRINT_DETAILS_ENDPOINT,
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
        self.server = server
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def search(self, request: PreprintLookupRequest) -> PreprintSearchResult:
        start_time = perf_counter()
        dois = request.dois[: request.max_results]
        if self._client is not None:
            return await self._search_with_client(
                request, dois, self._client, start_time
            )
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            return await self._search_with_client(request, dois, client, start_time)

    async def _search_with_client(
        self,
        request: PreprintLookupRequest,
        dois: list[str],
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PreprintSearchResult:
        semaphore = asyncio.Semaphore(request.concurrency)

        async def bounded_lookup(
            doi: str,
        ) -> tuple[PreprintArticle | None, int, PreprintLookupError | None]:
            async with semaphore:
                return await self._lookup_doi(
                    client=client,
                    doi=doi,
                    search_run_id=request.search_run_id,
                )

        outcomes = await asyncio.gather(*(bounded_lookup(doi) for doi in dois))
        records = [record for record, _, _ in outcomes if record is not None]
        retry_count = sum(retries for _, retries, _ in outcomes)
        errors = [error for _, _, error in outcomes if error is not None]
        if records and errors:
            status = PreprintSearchStatus.PARTIAL_SUCCESS
        elif records:
            status = PreprintSearchStatus.SUCCESS_WITH_RESULTS
        elif errors and len(errors) == len(outcomes):
            status = PreprintSearchStatus.FAILED
        else:
            status = PreprintSearchStatus.SUCCESS_EMPTY
        result = PreprintSearchResult(
            source=self.server.value,
            search_run_id=request.search_run_id,
            status=status,
            requested_count=len(dois),
            retrieved_count=len(records),
            failed_lookup_count=len(errors),
            retry_count=retry_count,
            records=records,
            latency_ms=(perf_counter() - start_time) * 1000,
            error=(
                PreprintLookupError(
                    error_type="all_lookups_failed",
                    message=f"{self.server.value} 所有 DOI 校验请求均失败",
                    retryable=any(error.retryable for error in errors),
                )
                if status == PreprintSearchStatus.FAILED
                else None
            ),
        )
        logger.bind(
            component=f"{self.server.value}_searcher",
            event="preprint_lookup_completed",
            search_run_id=request.search_run_id,
            server=self.server.value,
            status=result.status.value,
            requested_count=result.requested_count,
            retrieved_count=result.retrieved_count,
            failed_lookup_count=result.failed_lookup_count,
            retry_count=result.retry_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("预印本 DOI 校验完成")
        return result

    async def _lookup_doi(
        self,
        *,
        client: httpx.AsyncClient,
        doi: str,
        search_run_id: str,
    ) -> tuple[PreprintArticle | None, int, PreprintLookupError | None]:
        url = (
            f"{self.endpoint}/{self.server.value}/"
            f"{quote(doi, safe='/')}/na/json"
        )
        try:
            response, retry_count = await self._request(client=client, url=url)
            try:
                payload = response.json()
            except ValueError as exc:
                raise _PreprintFailure(
                    error=PreprintLookupError(
                        error_type="invalid_json",
                        message=f"{self.server.value} 未返回有效 JSON",
                    ),
                    retry_count=retry_count,
                ) from exc
            collection = payload.get("collection", [])
            if not isinstance(collection, list):
                raise _PreprintFailure(
                    error=PreprintLookupError(
                        error_type="invalid_response_schema",
                        message=f"{self.server.value} 响应缺少 collection 数组",
                    ),
                    retry_count=retry_count,
                )
            candidates = [item for item in collection if isinstance(item, dict)]
            if not candidates:
                return None, retry_count, None
            raw = max(candidates, key=lambda item: _optional_int(item.get("version")) or 0)
            return (
                _normalize_preprint(
                    raw,
                    expected_doi=doi,
                    server=self.server,
                    search_run_id=search_run_id,
                    retrieved_at=datetime.now(UTC),
                ),
                retry_count,
                None,
            )
        except _PreprintFailure as exc:
            return None, exc.retry_count, exc.error
        except Exception as exc:
            logger.bind(
                component=f"{self.server.value}_searcher",
                event="preprint_lookup_unexpected_error",
                search_run_id=search_run_id,
                error_type=type(exc).__name__,
            ).exception("预印本 DOI 响应处理失败")
            return (
                None,
                0,
                PreprintLookupError(
                    error_type=type(exc).__name__,
                    message=f"{self.server.value} DOI 响应处理失败",
                ),
            )

    async def _request(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[httpx.Response, int]:
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        await self._wait_before_retry(attempt)
                        continue
                    raise _PreprintFailure(
                        error=PreprintLookupError(
                            error_type="http_error",
                            message=(
                                f"{self.server.value} 返回 HTTP "
                                f"{response.status_code}"
                            ),
                            http_status=response.status_code,
                            retryable=True,
                        ),
                        retry_count=attempt,
                    )
                response.raise_for_status()
                return response, attempt
            except _PreprintFailure:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _PreprintFailure(
                    error=PreprintLookupError(
                        error_type=type(exc).__name__,
                        message=f"{self.server.value} 网络请求失败",
                        retryable=True,
                    ),
                    retry_count=attempt,
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise _PreprintFailure(
                    error=PreprintLookupError(
                        error_type="http_error",
                        message=(
                            f"{self.server.value} 返回 HTTP "
                            f"{exc.response.status_code}"
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


class BioRxivSearcher(PreprintSearcher):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(server=PreprintServer.BIORXIV, **kwargs)


class MedRxivSearcher(PreprintSearcher):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(server=PreprintServer.MEDRXIV, **kwargs)


def _normalize_preprint(
    raw: dict[str, Any],
    *,
    expected_doi: str,
    server: PreprintServer,
    search_run_id: str,
    retrieved_at: datetime,
) -> PreprintArticle:
    doi = _clean_text(raw.get("doi"))
    title = _clean_text(raw.get("title"))
    if not doi or doi.casefold() != expected_doi.casefold():
        raise ValueError("官方 API 返回的 DOI 与请求不一致")
    if not title:
        raise ValueError("预印本记录缺少题名")
    raw_server = _clean_text(raw.get("server"))
    if raw_server and raw_server.casefold() != server.value:
        raise ValueError("官方 API 返回的预印本服务器与请求不一致")
    author_text = _clean_text(raw.get("authors"))
    authors = [
        value.strip()
        for value in (author_text or "").replace("|", ";").split(";")
        if value.strip()
    ]
    publication_date = _clean_text(raw.get("date"))
    publication_year = (
        int(publication_date[:4])
        if publication_date and publication_date[:4].isdigit()
        else None
    )
    version = _optional_int(raw.get("version"))
    jatsxml_url = _http_url(raw.get("jatsxml"))
    published = _clean_text(raw.get("published"))
    published_doi = published if published and published.startswith("10.") else None
    host = "www.biorxiv.org" if server == PreprintServer.BIORXIV else "www.medrxiv.org"
    version_suffix = f"v{version}" if version else ""
    abstract = _clean_text(raw.get("abstract"))
    return PreprintArticle(
        source=server.value,
        source_record_id=doi,
        article_id=f"{server.value}:doi:{doi.casefold()}",
        doi=doi,
        title=title,
        abstract=abstract,
        abstract_available=abstract is not None,
        authors=authors,
        first_author=authors[0] if authors else None,
        corresponding_author=_clean_text(raw.get("author_corresponding")),
        corresponding_institution=_clean_text(
            raw.get("author_corresponding_institution")
        ),
        publication_date=publication_date,
        publication_year=publication_year,
        version=version,
        category=_clean_text(raw.get("category")),
        publication_type=_clean_text(raw.get("type")),
        license=_clean_text(raw.get("license")),
        jatsxml_url=jatsxml_url,
        published_doi=published_doi,
        preprint_server=server,
        has_full_text=jatsxml_url is not None,
        landing_url=f"https://{host}/content/{doi}{version_suffix}",
        retrieved_at=retrieved_at,
        search_run_id=search_run_id,
    )


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
