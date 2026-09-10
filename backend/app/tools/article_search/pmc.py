"""PMC 标识符解析、OA 资源发现与全文下载工具。

检索阶段只负责发现 PMCID 和题录信息；本模块在用户选择文献之后，先通过
PMC OA Service 解析官方资源链接，再下载 PDF 或其他可阅读格式。下载器不猜测
PMC 的文件路径，也不把“存在 PMCID”误认为“存在可下载全文”。

PMC OA Service 正在向新的 Cloud Service 迁移，因此网络访问被封装在
``PMCOAResolver`` 中。将来切换到 Cloud inventory 时，只需要替换解析器，
``PMCOADownloader`` 的下载、校验和日志逻辑可以继续复用。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PMC_ID_CONVERTER_ENDPOINT = (
    "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
)
PMC_OA_SERVICE_ENDPOINT = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"
PMC_ESEARCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PMC_EFETCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class PMCResourceFormat(StrEnum):
    """PMC OA Service 或 Cloud 资源的文件格式。"""

    PDF = "pdf"
    EPUB = "epub"
    HTML = "html"
    XML = "xml"
    TEXT = "text"
    TGZ = "tgz"
    OTHER = "other"


# Europe PMC 使用的名称与 PMC 资源模型兼容，方便上层统一处理。
FullTextFormat = PMCResourceFormat


class PMCResolutionStatus(StrEnum):
    """全文资源解析的结束状态。"""

    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    RETRACTED = "retracted"
    FAILED = "failed"


class PMCDownloadStatus(StrEnum):
    """全文下载的结束状态。"""

    SUCCESS = "success"
    NOT_AVAILABLE = "not_available"
    FAILED = "failed"


class PMCSearchSortMode(StrEnum):
    """PMC 关键词检索排序方式。"""

    RELEVANCE = "relevance"
    NEWEST = "newest"


class PMCSearchStatus(StrEnum):
    """一次 PMC 关键词检索的结束状态。"""

    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class PMCError(BaseModel):
    """对外暴露的脱敏错误摘要。"""

    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class PMCSearchRequest(BaseModel):
    """PMC ESearch + EFetch 检索请求。"""

    query: str = Field(min_length=1)
    page_size: int = Field(default=100, ge=1, le=100)
    max_results: int = Field(default=100, ge=1, le=100)
    sort_mode: PMCSearchSortMode = PMCSearchSortMode.RELEVANCE
    email: str | None = None
    api_key: str | None = None
    tool: str = Field(default="epi-research-agent", min_length=1, max_length=100)
    search_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @model_validator(mode="after")
    def normalize_text_fields(self) -> "PMCSearchRequest":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("PMC 检索式不能为空")
        self.email = self.email.strip() or None if self.email else None
        self.api_key = self.api_key.strip() or None if self.api_key else None
        self.tool = self.tool.strip()
        return self


class PMCAuthor(BaseModel):
    """PMC JATS 作者信息。"""

    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    collective_name: str | None = None


class PMCArticle(BaseModel):
    """供统一化和摘要筛选使用的 PMC 文献记录。"""

    source: str = "pmc"
    source_record_id: str
    article_id: str
    pmcid: str
    pmid: str | None = None
    doi: str | None = None
    title: str
    abstract: str | None = None
    abstract_available: bool = False
    authors: list[PMCAuthor] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    publication_types: list[str] = Field(default_factory=list)
    license: str | None = None
    is_open_access: bool | None = None
    has_full_text: bool = True
    can_download_full_text: bool = False
    landing_url: str
    retrieved_at: datetime
    search_run_id: str


class PMCSearchResult(BaseModel):
    """一次有界 PMC 关键词检索结果。"""

    source: str = "pmc"
    search_run_id: str
    status: PMCSearchStatus
    hit_count: int | None = None
    retrieved_count: int = 0
    invalid_record_count: int = 0
    page_count: int = 0
    retry_count: int = 0
    records: list[PMCArticle] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    error: PMCError | None = None


class PMCIdentifierRequest(BaseModel):
    """PMC ID Converter 请求；一次请求中的 ID 类型必须一致。"""

    ids: list[str] = Field(min_length=1, max_length=200)
    email: str | None = None
    tool: str = Field(default="epi-research-agent", min_length=1, max_length=100)

    @field_validator("ids")
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if not normalized:
            raise ValueError("至少需要一个 PMID、PMCID 或 DOI")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("ids 不能包含重复标识符")
        kinds = {_identifier_kind(value) for value in normalized}
        if len(kinds) > 1:
            raise ValueError("PMC ID Converter 一次请求只能使用同一种标识符")
        return normalized

    @model_validator(mode="after")
    def normalize_optional_fields(self) -> "PMCIdentifierRequest":
        self.email = self.email.strip() or None if self.email else None
        self.tool = self.tool.strip()
        return self


class PMCIdentifierRecord(BaseModel):
    """PMC ID Converter 返回的单条映射。"""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    requested_id: str | None = Field(default=None, alias="requested-id")
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    mid: str | None = None
    live: bool | None = None
    release_date: str | None = Field(default=None, alias="release-date")


class PMCIdentifierResult(BaseModel):
    """一次 PMC ID Converter 请求的结果。"""

    records: list[PMCIdentifierRecord] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    retry_count: int = Field(default=0, ge=0)
    error: PMCError | None = None


class PMCResource(BaseModel):
    """一篇 PMC 文献的一个可下载全文资源。"""

    resource_id: str
    format: PMCResourceFormat
    url: str
    original_format: str | None = None
    updated_at: str | None = None
    suggested_file_name: str
    is_downloadable: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not _is_http_url(value):
            raise ValueError("PMC 资源链接必须是 HTTP 或 HTTPS 地址")
        return value


class PMCOAResolution(BaseModel):
    """PMC OA 资源解析结果。"""

    pmcid: str
    status: PMCResolutionStatus
    license: str | None = None
    retracted: bool = False
    resources: list[PMCResource] = Field(default_factory=list)
    preferred_resource: PMCResource | None = None
    latency_ms: float = Field(ge=0.0)
    retry_count: int = Field(default=0, ge=0)
    error: PMCError | None = None


class PMCDownloadRequest(BaseModel):
    """用户确认后的全文下载请求。"""

    pmcid: str | None = None
    resource_url: str | None = None
    destination_dir: Path
    file_name: str | None = None
    overwrite: bool = False
    max_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    download_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @field_validator("pmcid")
    @classmethod
    def normalize_pmcid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_pmcid(value)

    @field_validator("resource_url")
    @classmethod
    def validate_resource_url(cls, value: str | None) -> str | None:
        if value is not None and not _is_http_url(value):
            raise ValueError("resource_url 必须是 HTTP 或 HTTPS 地址")
        return value

    @model_validator(mode="after")
    def require_source(self) -> "PMCDownloadRequest":
        if not self.pmcid and not self.resource_url:
            raise ValueError("pmcid 和 resource_url 至少提供一个")
        return self


class PMCDownloadResult(BaseModel):
    """全文下载结果和本地文件校验信息。"""

    pmcid: str | None
    status: PMCDownloadStatus
    format: PMCResourceFormat | None = None
    source_url: str | None = None
    local_path: str | None = None
    content_type: str | None = None
    bytes_written: int = Field(default=0, ge=0)
    sha256: str | None = None
    latency_ms: float = Field(ge=0.0)
    retry_count: int = Field(default=0, ge=0)
    error: PMCError | None = None


class _PMCRequestFailure(Exception):
    def __init__(
        self,
        *,
        error: PMCError,
        retry_count: int,
    ) -> None:
        super().__init__(error.message)
        self.error = error
        self.retry_count = retry_count


class PMCSearcher:
    """通过 NCBI E-utilities 在 PMC 中检索题录、摘要和全文页面。"""

    def __init__(
        self,
        *,
        esearch_endpoint: str = PMC_ESEARCH_ENDPOINT,
        efetch_endpoint: str = PMC_EFETCH_ENDPOINT,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _validate_network_options(timeout_seconds, max_retries, retry_base_seconds)
        self.esearch_endpoint = esearch_endpoint
        self.efetch_endpoint = efetch_endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def search(self, request: PMCSearchRequest) -> PMCSearchResult:
        """执行最多返回 100 篇的 PMC 关键词检索。"""

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
        request: PMCSearchRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PMCSearchResult:
        records: list[PMCArticle] = []
        offset = 0
        page_count = 0
        retry_count = 0
        invalid_record_count = 0
        hit_count: int | None = None
        try:
            while len(records) < request.max_results:
                page_size = min(
                    request.page_size,
                    request.max_results - len(records),
                )
                ids, hit_count, search_retries = await self._esearch(
                    client=client,
                    request=request,
                    retstart=offset,
                    retmax=page_size,
                )
                retry_count += search_retries
                page_count += 1
                if not ids:
                    break
                page_records, invalid_count, fetch_retries = await self._efetch(
                    client=client,
                    request=request,
                    pmc_numeric_ids=ids,
                )
                retry_count += fetch_retries
                invalid_record_count += invalid_count
                records.extend(page_records[: request.max_results - len(records)])
                offset += len(ids)
                if hit_count is None or offset >= hit_count or len(ids) < page_size:
                    break
        except _PMCRequestFailure as exc:
            retry_count += exc.retry_count
            result = self._build_result(
                request=request,
                status=(
                    PMCSearchStatus.PARTIAL_SUCCESS
                    if records
                    else PMCSearchStatus.FAILED
                ),
                records=records,
                hit_count=hit_count,
                invalid_record_count=invalid_record_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=exc.error,
            )
            self._log_search_result(result)
            return result
        except Exception as exc:
            logger.bind(
                component="pmc_searcher",
                event="pmc_search_unexpected_error",
                search_run_id=request.search_run_id,
                error_type=type(exc).__name__,
            ).exception("PMC 检索发生未预期错误")
            result = self._build_result(
                request=request,
                status=(
                    PMCSearchStatus.PARTIAL_SUCCESS
                    if records
                    else PMCSearchStatus.FAILED
                ),
                records=records,
                hit_count=hit_count,
                invalid_record_count=invalid_record_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=PMCError(
                    error_type=type(exc).__name__,
                    message="PMC 响应处理失败",
                ),
            )
            self._log_search_result(result)
            return result

        if invalid_record_count:
            status = PMCSearchStatus.PARTIAL_SUCCESS
        elif records:
            status = PMCSearchStatus.SUCCESS_WITH_RESULTS
        else:
            status = PMCSearchStatus.SUCCESS_EMPTY
        result = self._build_result(
            request=request,
            status=status,
            records=records,
            hit_count=hit_count,
            invalid_record_count=invalid_record_count,
            page_count=page_count,
            retry_count=retry_count,
            start_time=start_time,
        )
        self._log_search_result(result)
        return result

    async def _esearch(
        self,
        *,
        client: httpx.AsyncClient,
        request: PMCSearchRequest,
        retstart: int,
        retmax: int,
    ) -> tuple[list[str], int, int]:
        params = self._common_params(request)
        params.update(
            {
                "db": "pmc",
                "term": request.query,
                "retmode": "json",
                "retstart": str(retstart),
                "retmax": str(retmax),
                "sort": (
                    "pub date"
                    if request.sort_mode == PMCSearchSortMode.NEWEST
                    else "relevance"
                ),
            }
        )
        response, retries = await _request_with_retries(
            client=client,
            endpoint=self.esearch_endpoint,
            params=params,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            retry_base_seconds=self.retry_base_seconds,
            service_name="PMC ESearch",
        )
        try:
            payload = response.json()["esearchresult"]
            ids = [str(value) for value in payload.get("idlist", [])]
            hit_count = int(payload.get("count", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise _PMCRequestFailure(
                error=PMCError(
                    error_type="invalid_json",
                    message="PMC ESearch 未返回有效 JSON",
                ),
                retry_count=retries,
            ) from exc
        return ids, hit_count, retries

    async def _efetch(
        self,
        *,
        client: httpx.AsyncClient,
        request: PMCSearchRequest,
        pmc_numeric_ids: list[str],
    ) -> tuple[list[PMCArticle], int, int]:
        params = self._common_params(request)
        params.update(
            {
                "db": "pmc",
                "id": ",".join(pmc_numeric_ids),
                "retmode": "xml",
            }
        )
        response, retries = await _request_with_retries(
            client=client,
            endpoint=self.efetch_endpoint,
            params=params,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            retry_base_seconds=self.retry_base_seconds,
            service_name="PMC EFetch",
        )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise _PMCRequestFailure(
                error=PMCError(
                    error_type="invalid_xml",
                    message="PMC EFetch 未返回有效 JATS XML",
                ),
                retry_count=retries,
            ) from exc

        article_nodes = list(_iter_elements_by_local_name(root, "article"))
        retrieved_at = datetime.now(UTC)
        records: list[PMCArticle] = []
        invalid_count = 0
        for index, article_node in enumerate(article_nodes):
            fallback = (
                pmc_numeric_ids[index]
                if index < len(pmc_numeric_ids)
                else None
            )
            try:
                records.append(
                    _normalize_pmc_article(
                        article_node,
                        fallback_numeric_id=fallback,
                        search_run_id=request.search_run_id,
                        retrieved_at=retrieved_at,
                    )
                )
            except ValueError:
                invalid_count += 1
        return records, invalid_count, retries

    @staticmethod
    def _common_params(request: PMCSearchRequest) -> dict[str, str]:
        params = {"tool": request.tool}
        if request.email:
            params["email"] = request.email
        if request.api_key:
            params["api_key"] = request.api_key
        return params

    @staticmethod
    def _build_result(
        *,
        request: PMCSearchRequest,
        status: PMCSearchStatus,
        records: list[PMCArticle],
        hit_count: int | None,
        invalid_record_count: int,
        page_count: int,
        retry_count: int,
        start_time: float,
        error: PMCError | None = None,
    ) -> PMCSearchResult:
        return PMCSearchResult(
            search_run_id=request.search_run_id,
            status=status,
            hit_count=hit_count,
            retrieved_count=len(records),
            invalid_record_count=invalid_record_count,
            page_count=page_count,
            retry_count=retry_count,
            records=records,
            latency_ms=(perf_counter() - start_time) * 1000,
            error=error,
        )

    @staticmethod
    def _log_search_result(result: PMCSearchResult) -> None:
        logger.bind(
            component="pmc_searcher",
            event="pmc_search_completed",
            search_run_id=result.search_run_id,
            status=result.status.value,
            hit_count=result.hit_count,
            retrieved_count=result.retrieved_count,
            invalid_record_count=result.invalid_record_count,
            page_count=result.page_count,
            retry_count=result.retry_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("PMC 检索完成")


class PMCIdentifierResolver:
    """通过 PMC ID Converter 将 PMID、DOI 或 PMCID 统一为 PMCID。"""

    def __init__(
        self,
        *,
        endpoint: str = PMC_ID_CONVERTER_ENDPOINT,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _validate_network_options(timeout_seconds, max_retries, retry_base_seconds)
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def resolve(
        self,
        request: PMCIdentifierRequest,
    ) -> PMCIdentifierResult:
        start_time = perf_counter()
        if self._client is not None:
            return await self._resolve_with_client(request, self._client, start_time)

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            return await self._resolve_with_client(request, client, start_time)

    async def _resolve_with_client(
        self,
        request: PMCIdentifierRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PMCIdentifierResult:
        params: dict[str, str] = {
            "ids": ",".join(request.ids),
            "format": "json",
            "tool": request.tool,
        }
        if request.email:
            params["email"] = request.email

        try:
            response, retry_count = await _request_with_retries(
                client=client,
                endpoint=self.endpoint,
                params=params,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                retry_base_seconds=self.retry_base_seconds,
                service_name="PMC ID Converter",
            )
            payload = response.json()
            raw_records = payload.get("records", [])
            if not isinstance(raw_records, list):
                raise ValueError("records 不是数组")
            records = [PMCIdentifierRecord.model_validate(item) for item in raw_records]
            result = PMCIdentifierResult(
                records=records,
                latency_ms=(perf_counter() - start_time) * 1000,
                retry_count=retry_count,
            )
            logger.bind(
                component="pmc_identifier_resolver",
                event="pmc_identifier_resolution_completed",
                requested_count=len(request.ids),
                resolved_count=sum(record.pmcid is not None for record in records),
                retry_count=retry_count,
                latency_ms=round(result.latency_ms, 1),
            ).info("PMC 标识符解析完成")
            return result
        except _PMCRequestFailure as exc:
            return PMCIdentifierResult(
                latency_ms=(perf_counter() - start_time) * 1000,
                retry_count=exc.retry_count,
                error=exc.error,
            )
        except Exception as exc:
            logger.bind(
                component="pmc_identifier_resolver",
                event="pmc_identifier_resolution_failed",
                error_type=type(exc).__name__,
            ).exception("PMC 标识符响应处理失败")
            return PMCIdentifierResult(
                latency_ms=(perf_counter() - start_time) * 1000,
                error=PMCError(
                    error_type=type(exc).__name__,
                    message="PMC ID Converter 响应处理失败",
                ),
            )


# 简短别名，便于上层按接口名称使用。
PMCIdConverter = PMCIdentifierResolver


class PMCOAResolver:
    """通过 PMC OA Service 解析一个 PMCID 的可下载资源。"""

    def __init__(
        self,
        *,
        endpoint: str = PMC_OA_SERVICE_ENDPOINT,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _validate_network_options(timeout_seconds, max_retries, retry_base_seconds)
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def resolve(self, pmcid: str) -> PMCOAResolution:
        normalized_pmcid = normalize_pmcid(pmcid)
        start_time = perf_counter()
        if self._client is not None:
            return await self._resolve_with_client(
                normalized_pmcid, self._client, start_time
            )

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            return await self._resolve_with_client(
                normalized_pmcid, client, start_time
            )

    async def _resolve_with_client(
        self,
        pmcid: str,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PMCOAResolution:
        try:
            response, retry_count = await _request_with_retries(
                client=client,
                endpoint=self.endpoint,
                params={"id": pmcid},
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                retry_base_seconds=self.retry_base_seconds,
                service_name="PMC OA Service",
            )
            root = ET.fromstring(response.content)
            record = next(_iter_elements_by_local_name(root, "record"), None)
            if record is None:
                result = PMCOAResolution(
                    pmcid=pmcid,
                    status=PMCResolutionStatus.NOT_AVAILABLE,
                    latency_ms=(perf_counter() - start_time) * 1000,
                    retry_count=retry_count,
                )
                self._log_resolution(result)
                return result

            license_name = _clean_text(record.attrib.get("license"))
            retracted = _parse_bool(record.attrib.get("retracted")) is True
            resources = _parse_oa_resources(record, pmcid)
            preferred = _select_preferred_resource(resources)
            status = (
                PMCResolutionStatus.RETRACTED
                if retracted
                else (
                    PMCResolutionStatus.AVAILABLE
                    if resources
                    else PMCResolutionStatus.NOT_AVAILABLE
                )
            )
            result = PMCOAResolution(
                pmcid=pmcid,
                status=status,
                license=license_name,
                retracted=retracted,
                resources=resources,
                preferred_resource=preferred,
                latency_ms=(perf_counter() - start_time) * 1000,
                retry_count=retry_count,
            )
            self._log_resolution(result)
            return result
        except ET.ParseError:
            result = PMCOAResolution(
                pmcid=pmcid,
                status=PMCResolutionStatus.FAILED,
                latency_ms=(perf_counter() - start_time) * 1000,
                error=PMCError(
                    error_type="invalid_xml",
                    message="PMC OA Service 未返回有效 XML",
                ),
            )
            self._log_resolution(result)
            return result
        except _PMCRequestFailure as exc:
            result = PMCOAResolution(
                pmcid=pmcid,
                status=PMCResolutionStatus.FAILED,
                latency_ms=(perf_counter() - start_time) * 1000,
                retry_count=exc.retry_count,
                error=exc.error,
            )
            self._log_resolution(result)
            return result
        except Exception as exc:
            logger.bind(
                component="pmc_oa_resolver",
                event="pmc_oa_resolution_failed",
                pmcid=pmcid,
                error_type=type(exc).__name__,
            ).exception("PMC OA 资源解析失败")
            result = PMCOAResolution(
                pmcid=pmcid,
                status=PMCResolutionStatus.FAILED,
                latency_ms=(perf_counter() - start_time) * 1000,
                error=PMCError(
                    error_type=type(exc).__name__,
                    message="PMC OA Service 响应处理失败",
                ),
            )
            self._log_resolution(result)
            return result

    @staticmethod
    def _log_resolution(result: PMCOAResolution) -> None:
        logger.bind(
            component="pmc_oa_resolver",
            event="pmc_oa_resolution_completed",
            pmcid=result.pmcid,
            status=result.status.value,
            resource_count=len(result.resources),
            preferred_format=(
                result.preferred_resource.format.value
                if result.preferred_resource
                else None
            ),
            retry_count=result.retry_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("PMC OA 资源解析完成")


class PMCOADownloader:
    """下载 PMC OA 资源并以临时文件原子写入本地。"""

    def __init__(
        self,
        *,
        resolver: PMCOAResolver | None = None,
        timeout_seconds: float = 120.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _validate_network_options(timeout_seconds, max_retries, retry_base_seconds)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client
        self._resolver = resolver

    async def download(self, request: PMCDownloadRequest) -> PMCDownloadResult:
        """解析资源并下载到 ``destination_dir``。"""

        start_time = perf_counter()
        if self._client is not None:
            return await self._download_with_client(request, self._client, start_time)

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            return await self._download_with_client(request, client, start_time)

    async def download_by_pmcid(
        self,
        *,
        pmcid: str,
        destination_dir: Path,
        file_name: str | None = None,
        overwrite: bool = False,
        max_bytes: int = 200 * 1024 * 1024,
    ) -> PMCDownloadResult:
        """提供一个不需要手动构造 Pydantic 请求的便捷入口。"""

        return await self.download(
            PMCDownloadRequest(
                pmcid=pmcid,
                destination_dir=destination_dir,
                file_name=file_name,
                overwrite=overwrite,
                max_bytes=max_bytes,
            )
        )

    async def _download_with_client(
        self,
        request: PMCDownloadRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PMCDownloadResult:
        pmcid = request.pmcid
        resource: PMCResource | None = None
        retry_count = 0
        try:
            if request.resource_url:
                resource = _resource_from_direct_url(request.resource_url, pmcid)
            else:
                resolver = self._resolver or PMCOAResolver(
                    timeout_seconds=self.timeout_seconds,
                    max_retries=self.max_retries,
                    retry_base_seconds=self.retry_base_seconds,
                    client=client,
                )
                resolution = await resolver.resolve(pmcid or "")
                retry_count += resolution.retry_count
                if resolution.preferred_resource is None:
                    status = (
                        PMCDownloadStatus.FAILED
                        if resolution.status == PMCResolutionStatus.FAILED
                        else PMCDownloadStatus.NOT_AVAILABLE
                    )
                    return PMCDownloadResult(
                        pmcid=pmcid,
                        status=status,
                        latency_ms=(perf_counter() - start_time) * 1000,
                        retry_count=retry_count,
                        error=resolution.error
                        or PMCError(
                            error_type="full_text_unavailable",
                            message="PMC 没有可下载的全文资源",
                        ),
                    )
                resource = resolution.preferred_resource

            assert resource is not None
            target = _target_path(request, resource)
            if target.exists() and not request.overwrite:
                return self._failed_result(
                    request=request,
                    resource=resource,
                    start_time=start_time,
                    retry_count=retry_count,
                    error=PMCError(
                        error_type="file_exists",
                        message="目标文件已存在；需要 overwrite=True 才能覆盖",
                    ),
                )
            target.parent.mkdir(parents=True, exist_ok=True)

            response, request_retries, bytes_written, digest, content_type = (
                await self._stream_to_file(
                    client=client,
                    resource=resource,
                    target=target,
                    max_bytes=request.max_bytes,
                )
            )
            retry_count += request_retries
            result = PMCDownloadResult(
                pmcid=pmcid,
                status=PMCDownloadStatus.SUCCESS,
                format=resource.format,
                source_url=str(response.url),
                local_path=str(target),
                content_type=content_type,
                bytes_written=bytes_written,
                sha256=digest,
                latency_ms=(perf_counter() - start_time) * 1000,
                retry_count=retry_count,
            )
            logger.bind(
                component="pmc_oa_downloader",
                event="pmc_full_text_download_completed",
                pmcid=pmcid,
                format=resource.format.value,
                bytes_written=bytes_written,
                retry_count=retry_count,
                latency_ms=round(result.latency_ms, 1),
            ).info("PMC 全文下载完成")
            return result
        except _PMCRequestFailure as exc:
            return self._failed_result(
                request=request,
                resource=resource,
                start_time=start_time,
                retry_count=retry_count + exc.retry_count,
                error=exc.error,
            )
        except Exception as exc:
            logger.bind(
                component="pmc_oa_downloader",
                event="pmc_full_text_download_failed",
                pmcid=pmcid,
                error_type=type(exc).__name__,
            ).exception("PMC 全文下载失败")
            return self._failed_result(
                request=request,
                resource=resource,
                start_time=start_time,
                retry_count=retry_count,
                error=PMCError(
                    error_type=type(exc).__name__,
                    message="PMC 全文下载失败",
                ),
            )

    async def _stream_to_file(
        self,
        *,
        client: httpx.AsyncClient,
        resource: PMCResource,
        target: Path,
        max_bytes: int,
    ) -> tuple[httpx.Response, int, int, str, str | None]:
        for attempt in range(self.max_retries + 1):
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.part")
            try:
                async with client.stream(
                    "GET",
                    resource.url,
                    headers={"Accept": "application/octet-stream, */*"},
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.max_retries:
                            await self._wait_before_retry(attempt)
                            continue
                        raise _PMCRequestFailure(
                            error=PMCError(
                                error_type="http_error",
                                message=f"PMC 下载返回 HTTP {response.status_code}",
                                http_status=response.status_code,
                                retryable=True,
                            ),
                            retry_count=attempt,
                        )
                    response.raise_for_status()
                    content_length = _content_length(response.headers.get("content-length"))
                    if content_length is not None and content_length > max_bytes:
                        raise _PMCRequestFailure(
                            error=PMCError(
                                error_type="file_too_large",
                                message="PMC 全文超过允许的最大文件大小",
                                retryable=False,
                            ),
                            retry_count=attempt,
                        )

                    digest = hashlib.sha256()
                    total = 0
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > max_bytes:
                                raise _PMCRequestFailure(
                                    error=PMCError(
                                        error_type="file_too_large",
                                        message="PMC 全文超过允许的最大文件大小",
                                    ),
                                    retry_count=attempt,
                                )
                            digest.update(chunk)
                            output.write(chunk)
                    if total == 0:
                        raise _PMCRequestFailure(
                            error=PMCError(
                                error_type="empty_file",
                                message="PMC 下载返回空文件",
                            ),
                            retry_count=attempt,
                        )
                    temporary.replace(target)
                    return (
                        response,
                        attempt,
                        total,
                        digest.hexdigest(),
                        response.headers.get("content-type"),
                    )
            except _PMCRequestFailure:
                temporary.unlink(missing_ok=True)
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                temporary.unlink(missing_ok=True)
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _PMCRequestFailure(
                    error=PMCError(
                        error_type=type(exc).__name__,
                        message="PMC 全文下载网络请求失败",
                        retryable=True,
                    ),
                    retry_count=attempt,
                ) from exc
            except httpx.HTTPStatusError as exc:
                temporary.unlink(missing_ok=True)
                raise _PMCRequestFailure(
                    error=PMCError(
                        error_type="http_error",
                        message=f"PMC 下载返回 HTTP {exc.response.status_code}",
                        http_status=exc.response.status_code,
                        retryable=False,
                    ),
                    retry_count=attempt,
                ) from exc
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        raise RuntimeError("unreachable")

    async def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        jitter = random.uniform(0, delay * 0.25) if delay else 0
        await asyncio.sleep(delay + jitter)

    @staticmethod
    def _failed_result(
        *,
        request: PMCDownloadRequest,
        resource: PMCResource | None,
        start_time: float,
        retry_count: int,
        error: PMCError,
    ) -> PMCDownloadResult:
        return PMCDownloadResult(
            pmcid=request.pmcid,
            status=PMCDownloadStatus.FAILED,
            format=resource.format if resource else None,
            source_url=resource.url if resource else request.resource_url,
            latency_ms=(perf_counter() - start_time) * 1000,
            retry_count=retry_count,
            error=error,
        )


# 语义别名：后续接入 Cloud inventory 时可以保留同一个下载器调用方式。
PMCFullTextDownloader = PMCOADownloader


async def _request_with_retries(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict[str, str],
    timeout_seconds: float,
    max_retries: int,
    retry_base_seconds: float,
    service_name: str,
) -> tuple[httpx.Response, int]:
    for attempt in range(max_retries + 1):
        try:
            response = await client.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json, application/xml"},
                timeout=timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < max_retries:
                    await _wait_before_retry(retry_base_seconds, attempt)
                    continue
                raise _PMCRequestFailure(
                    error=PMCError(
                        error_type="http_error",
                        message=f"{service_name} 返回 HTTP {response.status_code}",
                        http_status=response.status_code,
                        retryable=True,
                    ),
                    retry_count=attempt,
                )
            response.raise_for_status()
            return response, attempt
        except _PMCRequestFailure:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt < max_retries:
                await _wait_before_retry(retry_base_seconds, attempt)
                continue
            raise _PMCRequestFailure(
                error=PMCError(
                    error_type=type(exc).__name__,
                    message=f"{service_name} 网络请求失败",
                    retryable=True,
                ),
                retry_count=attempt,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise _PMCRequestFailure(
                error=PMCError(
                    error_type="http_error",
                    message=f"{service_name} 返回 HTTP {exc.response.status_code}",
                    http_status=exc.response.status_code,
                    retryable=False,
                ),
                retry_count=attempt,
            ) from exc
    raise RuntimeError("unreachable")


async def _wait_before_retry(base_seconds: float, attempt: int) -> None:
    delay = base_seconds * (2**attempt)
    jitter = random.uniform(0, delay * 0.25) if delay else 0
    await asyncio.sleep(delay + jitter)


def _parse_oa_resources(record: ET.Element, pmcid: str) -> list[PMCResource]:
    resources: list[PMCResource] = []
    seen: set[str] = set()
    for link in _iter_elements_by_local_name(record, "link"):
        raw_url = _clean_text(link.attrib.get("href"))
        if not raw_url:
            continue
        url = _normalize_download_url(raw_url)
        if not url or url.casefold() in seen:
            continue
        seen.add(url.casefold())
        raw_format = _clean_text(link.attrib.get("format"))
        format_ = _detect_resource_format(raw_format, url)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        resources.append(
            PMCResource(
                resource_id=f"{pmcid}:{format_.value}:{digest}",
                format=format_,
                url=url,
                original_format=raw_format,
                updated_at=_clean_text(link.attrib.get("updated")),
                suggested_file_name=_suggested_file_name(pmcid, format_, url),
            )
        )
    return resources


def _select_preferred_resource(resources: list[PMCResource]) -> PMCResource | None:
    rank = {
        PMCResourceFormat.PDF: 0,
        PMCResourceFormat.EPUB: 1,
        PMCResourceFormat.XML: 2,
        PMCResourceFormat.HTML: 3,
        PMCResourceFormat.TEXT: 4,
        PMCResourceFormat.TGZ: 5,
        PMCResourceFormat.OTHER: 6,
    }
    return min(resources, key=lambda item: rank[item.format]) if resources else None


def _resource_from_direct_url(url: str, pmcid: str | None) -> PMCResource:
    normalized_pmcid = pmcid or "pmc_article"
    format_ = _detect_resource_format(None, url)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return PMCResource(
        resource_id=f"{normalized_pmcid}:{format_.value}:{digest}",
        format=format_,
        url=url,
        suggested_file_name=_suggested_file_name(normalized_pmcid, format_, url),
    )


def _target_path(request: PMCDownloadRequest, resource: PMCResource) -> Path:
    raw_name = request.file_name or resource.suggested_file_name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw_name).name).strip(".")
    if not safe_name:
        safe_name = resource.suggested_file_name
    return request.destination_dir / safe_name


def _normalize_pmc_article(
    article: ET.Element,
    *,
    fallback_numeric_id: str | None,
    search_run_id: str,
    retrieved_at: datetime,
) -> PMCArticle:
    ids: dict[str, str] = {}
    for node in _iter_elements_by_local_name(article, "article-id"):
        id_type = (node.attrib.get("pub-id-type") or "").casefold()
        value = _element_text(node)
        if id_type and value:
            ids[id_type] = value

    raw_pmcid = ids.get("pmcid") or ids.get("pmc") or fallback_numeric_id
    if not raw_pmcid:
        raise ValueError("PMC 文献记录缺少 PMCID")
    if raw_pmcid.upper().startswith("PMC"):
        pmcid = normalize_pmcid(raw_pmcid)
    elif raw_pmcid.isdigit():
        pmcid = normalize_pmcid(f"PMC{raw_pmcid}")
    else:
        raise ValueError("PMC 文献记录包含无效 PMCID")

    title_node = next(_iter_elements_by_local_name(article, "article-title"), None)
    title = _element_text(title_node)
    if not title:
        raise ValueError("PMC 文献记录缺少题名")
    abstract = _pmc_abstract(article)
    authors = _pmc_authors(article)
    publication_date, publication_year = _pmc_publication_date(article)
    article_type = _clean_text(article.attrib.get("article-type"))
    publication_types = _deduplicate_texts(
        [
            article_type,
            *[
                _element_text(node)
                for node in _iter_elements_by_local_name(article, "subject")
            ],
        ]
    )
    license_name = _pmc_license(article)
    return PMCArticle(
        source_record_id=pmcid,
        article_id=f"pmc:pmcid:{pmcid}",
        pmcid=pmcid,
        pmid=ids.get("pmid"),
        doi=ids.get("doi"),
        title=title,
        abstract=abstract,
        abstract_available=abstract is not None,
        authors=authors,
        first_author=authors[0].full_name if authors else None,
        journal_title=_element_text(
            next(_iter_elements_by_local_name(article, "journal-title"), None)
        ),
        publication_date=publication_date,
        publication_year=publication_year,
        publication_types=publication_types,
        license=license_name,
        is_open_access=None,
        has_full_text=True,
        can_download_full_text=False,
        landing_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
        retrieved_at=retrieved_at,
        search_run_id=search_run_id,
    )


def _pmc_authors(article: ET.Element) -> list[PMCAuthor]:
    result: list[PMCAuthor] = []
    for contrib in _iter_elements_by_local_name(article, "contrib"):
        contrib_type = (contrib.attrib.get("contrib-type") or "author").casefold()
        if contrib_type != "author":
            continue
        collective = _element_text(
            next(_iter_elements_by_local_name(contrib, "collab"), None)
        )
        surname = _element_text(
            next(_iter_elements_by_local_name(contrib, "surname"), None)
        )
        given_names = _element_text(
            next(_iter_elements_by_local_name(contrib, "given-names"), None)
        )
        full_name = collective or " ".join(
            part for part in (given_names, surname) if part
        )
        if full_name:
            result.append(
                PMCAuthor(
                    full_name=full_name,
                    first_name=given_names,
                    last_name=surname,
                    collective_name=collective,
                )
            )
    return result


def _pmc_abstract(article: ET.Element) -> str | None:
    abstract = next(_iter_elements_by_local_name(article, "abstract"), None)
    if abstract is None:
        return None
    sections: list[str] = []
    for section in _direct_children_by_local_name(abstract, "sec"):
        title = _element_text(
            next(_iter_elements_by_local_name(section, "title"), None)
        )
        paragraphs = [
            _element_text(node)
            for node in _iter_elements_by_local_name(section, "p")
        ]
        body = " ".join(value for value in paragraphs if value)
        text = ": ".join(value for value in (title, body) if value)
        if text:
            sections.append(text)
    if sections:
        return "\n".join(sections)
    paragraphs = [
        _element_text(node)
        for node in _iter_elements_by_local_name(abstract, "p")
    ]
    text = "\n".join(value for value in paragraphs if value)
    return text or _element_text(abstract)


def _pmc_publication_date(article: ET.Element) -> tuple[str | None, int | None]:
    dates = list(_iter_elements_by_local_name(article, "pub-date"))
    if not dates:
        return None, None
    preferred = next(
        (
            node
            for node in dates
            if (node.attrib.get("pub-type") or node.attrib.get("publication-format"))
            in {"epub", "electronic"}
        ),
        dates[0],
    )
    year = _element_text(
        next(_direct_children_by_local_name(preferred, "year"), None)
    )
    month = _element_text(
        next(_direct_children_by_local_name(preferred, "month"), None)
    )
    day = _element_text(next(_direct_children_by_local_name(preferred, "day"), None))
    publication_year = int(year) if year and year.isdigit() else None
    if not year:
        return None, publication_year
    if month and month.isdigit():
        month = month.zfill(2)
    if day and day.isdigit():
        day = day.zfill(2)
    return "-".join(part for part in (year, month, day) if part), publication_year


def _pmc_license(article: ET.Element) -> str | None:
    license_node = next(_iter_elements_by_local_name(article, "license"), None)
    if license_node is None:
        return None
    href = (
        license_node.attrib.get("{http://www.w3.org/1999/xlink}href")
        or license_node.attrib.get("href")
    )
    return _clean_text(href) or _element_text(license_node)


def _element_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = " ".join("".join(element.itertext()).split())
    return value or None


def _direct_children_by_local_name(
    element: ET.Element,
    local_name: str,
):
    for child in list(element):
        if child.tag.rsplit("}", 1)[-1].casefold() == local_name.casefold():
            yield child


def _deduplicate_texts(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value)
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            result.append(cleaned)
    return result


def normalize_pmcid(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"PMC\d+", normalized):
        raise ValueError("PMCID 必须符合 PMC 后跟数字的格式")
    return normalized


def _identifier_kind(value: str) -> str:
    normalized = value.strip()
    if re.fullmatch(r"PMC\d+", normalized, flags=re.IGNORECASE):
        return "pmcid"
    if normalized.isdigit():
        return "pmid"
    if "/" in normalized or normalized.casefold().startswith("10."):
        return "doi"
    raise ValueError("标识符必须是 PMID、PMCID 或 DOI")


def _iter_elements_by_local_name(
    root: ET.Element,
    local_name: str,
):
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() == local_name.casefold():
            yield element


def _detect_resource_format(raw_format: str | None, url: str) -> PMCResourceFormat:
    text = f"{raw_format or ''} {urlparse(url).path}".casefold()
    if "pdf" in text:
        return PMCResourceFormat.PDF
    if "epub" in text:
        return PMCResourceFormat.EPUB
    if "html" in text or "htm" in text:
        return PMCResourceFormat.HTML
    if "xml" in text:
        return PMCResourceFormat.XML
    if "tgz" in text or "tar.gz" in text or "tarball" in text:
        return PMCResourceFormat.TGZ
    if "text" in text or text.endswith(".txt"):
        return PMCResourceFormat.TEXT
    return PMCResourceFormat.OTHER


def _suggested_file_name(
    pmcid: str,
    format_: PMCResourceFormat,
    url: str,
) -> str:
    extension = {
        PMCResourceFormat.PDF: "pdf",
        PMCResourceFormat.EPUB: "epub",
        PMCResourceFormat.HTML: "html",
        PMCResourceFormat.XML: "xml",
        PMCResourceFormat.TEXT: "txt",
        PMCResourceFormat.TGZ: "tar.gz",
        PMCResourceFormat.OTHER: "bin",
    }[format_]
    if format_ == PMCResourceFormat.OTHER:
        suffix = Path(urlparse(url).path).suffix.lstrip(".")
        if suffix and re.fullmatch(r"[A-Za-z0-9]{1,10}", suffix):
            extension = suffix
    return f"{pmcid}.{extension}"


def _normalize_download_url(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme == "ftp" and parsed.netloc:
        # NCBI 当前 FTP 资源可通过同一路径的 HTTPS 端点读取。
        return f"https://{parsed.netloc}{parsed.path}"
    return None


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_bool(value: Any) -> bool | None:
    normalized = str(value).strip().casefold() if value is not None else ""
    if normalized in {"yes", "true", "1", "y"}:
        return True
    if normalized in {"no", "false", "0", "n"}:
        return False
    return None


def _content_length(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _validate_network_options(
    timeout_seconds: float,
    max_retries: int,
    retry_base_seconds: float,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    if max_retries < 0:
        raise ValueError("max_retries 不能小于 0")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds 不能小于 0")
