"""Europe PMC 异步文献检索与全文资源发现工具。"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from time import perf_counter
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


EUROPE_PMC_SEARCH_ENDPOINT = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/searchPOST"
)


class EuropePMCSearchStatus(StrEnum):
    """一次 Europe PMC 检索的可观测结束状态。"""

    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class FullTextFormat(StrEnum):
    """Europe PMC 全文资源的可阅读格式。"""

    PDF = "pdf"
    EPUB = "epub"
    HTML = "html"
    XML = "xml"
    TEXT = "text"
    OTHER = "other"


class EuropePMCSearchRequest(BaseModel):
    """Europe PMC 检索请求；query 必须已经由数据库专属编译器生成。"""

    query: str = Field(min_length=1)
    page_size: int = Field(default=100, ge=1, le=100)
    max_results: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=100)
    synonym: bool = False
    email: str | None = None
    search_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @model_validator(mode="after")
    def normalize_text_fields(self) -> "EuropePMCSearchRequest":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("Europe PMC 检索式不能为空")
        if self.email is not None:
            self.email = self.email.strip() or None
        return self


class EuropePMCAuthor(BaseModel):
    """标准化作者基础信息。"""

    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    initials: str | None = None
    orcid: str | None = None


class FullTextResource(BaseModel):
    """一篇文献的一个具体全文表示形式。"""

    resource_id: str
    format: FullTextFormat
    original_style: str | None = None
    url: str
    site: str | None = None
    availability: str | None = None
    availability_code: str | None = None
    is_open_access: bool | None = None
    is_downloadable: bool | None = None
    suggested_file_name: str


class EuropePMCArticle(BaseModel):
    """供后续统一化、摘要筛选和用户选择使用的文献记录。"""

    source: str = "europe_pmc"
    source_record_id: str
    source_database_code: str | None = None
    article_id: str
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None

    title: str | None = None
    authors: list[EuropePMCAuthor] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    journal_abbreviation: str | None = None
    issn: str | None = None
    electronic_issn: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    publication_types: list[str] = Field(default_factory=list)
    study_design: str | None = None
    publication_status: str | None = None
    language: str | None = None
    keywords: list[str] = Field(default_factory=list)

    abstract_raw: str | None = None
    abstract: str | None = None
    abstract_available: bool = False

    is_open_access: bool | None = None
    in_europe_pmc: bool | None = None
    in_pmc: bool | None = None
    has_pdf: bool | None = None
    has_full_text: bool = False
    can_download_full_text: bool = False
    license: str | None = None
    landing_url: str | None = None
    full_text_resources: list[FullTextResource] = Field(default_factory=list)
    preferred_full_text_format: FullTextFormat | None = None
    preferred_download_url: str | None = None
    cited_by_count: int | None = None
    retrieved_at: datetime
    search_run_id: str


class EuropePMCSearchError(BaseModel):
    """不会包含原始医学检索式或上游响应正文的错误摘要。"""

    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class EuropePMCSearchResult(BaseModel):
    """一次有界 Europe PMC 检索的完整结果。"""

    source: str = "europe_pmc"
    search_run_id: str
    status: EuropePMCSearchStatus
    hit_count: int | None = None
    retrieved_count: int = 0
    page_count: int = 0
    retry_count: int = 0
    records: list[EuropePMCArticle] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    error: EuropePMCSearchError | None = None


class EuropePMCRawResultList(BaseModel):
    """Europe PMC `resultList` 原始容器。"""

    model_config = ConfigDict(extra="allow")

    result: list[dict[str, Any]] = Field(default_factory=list)


class EuropePMCRawSearchPage(BaseModel):
    """Europe PMC 搜索页顶层契约。"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    version: str | None = None
    hit_count: int = Field(alias="hitCount", ge=0)
    next_cursor_mark: str | None = Field(default=None, alias="nextCursorMark")
    result_list: EuropePMCRawResultList = Field(alias="resultList")


class _PlainTextHTMLParser(HTMLParser):
    """将 Europe PMC 摘要中的 HTML 标题和段落转换为可读纯文本。"""

    _BLOCK_TAGS = {"br", "p", "div", "h1", "h2", "h3", "h4", "li"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str | None:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        value = "\n".join(line for line in lines if line)
        return value or None


class _EuropePMCRequestFailure(Exception):
    def __init__(
        self,
        *,
        error_type: str,
        message: str,
        retry_count: int,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error = EuropePMCSearchError(
            error_type=error_type,
            message=message,
            http_status=http_status,
            retryable=retryable,
        )
        self.retry_count = retry_count


class EuropePMCSearcher:
    """通过 Europe PMC REST API 搜索文献并发现可用全文资源。"""

    def __init__(
        self,
        *,
        endpoint: str = EUROPE_PMC_SEARCH_ENDPOINT,
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

        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def search(
        self,
        request: EuropePMCSearchRequest,
    ) -> EuropePMCSearchResult:
        """执行有界游标分页检索，不下载任何全文文件。"""

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
        request: EuropePMCSearchRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> EuropePMCSearchResult:
        records: list[EuropePMCArticle] = []
        cursor_mark = "*"
        page_count = 0
        retry_count = 0
        hit_count: int | None = None

        try:
            while (
                len(records) < request.max_results
                and page_count < request.max_pages
            ):
                remaining = request.max_results - len(records)
                page_size = min(request.page_size, remaining)
                page, page_retries = await self._request_page(
                    client=client,
                    request=request,
                    cursor_mark=cursor_mark,
                    page_size=page_size,
                )
                retry_count += page_retries
                page_count += 1
                hit_count = page.hit_count

                if not page.result_list.result:
                    break

                retrieved_at = datetime.now(UTC)
                records.extend(
                    _normalize_article(
                        raw_record,
                        search_run_id=request.search_run_id,
                        retrieved_at=retrieved_at,
                    )
                    for raw_record in page.result_list.result[:remaining]
                )

                next_cursor_mark = page.next_cursor_mark
                if not next_cursor_mark or next_cursor_mark == cursor_mark:
                    break
                cursor_mark = next_cursor_mark
        except _EuropePMCRequestFailure as exc:
            retry_count += exc.retry_count
            status = (
                EuropePMCSearchStatus.PARTIAL_SUCCESS
                if records
                else EuropePMCSearchStatus.FAILED
            )
            result = _build_search_result(
                request=request,
                status=status,
                records=records,
                hit_count=hit_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=exc.error,
            )
            _log_search_result(result, request)
            return result
        except Exception as exc:
            logger.bind(
                component="europe_pmc_searcher",
                event="europe_pmc_search_unexpected_error",
                search_run_id=request.search_run_id,
                query_length=len(request.query),
                error_type=type(exc).__name__,
            ).exception("Europe PMC 检索发生未预期错误")
            result = _build_search_result(
                request=request,
                status=(
                    EuropePMCSearchStatus.PARTIAL_SUCCESS
                    if records
                    else EuropePMCSearchStatus.FAILED
                ),
                records=records,
                hit_count=hit_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=EuropePMCSearchError(
                    error_type=type(exc).__name__,
                    message="Europe PMC 响应处理失败",
                ),
            )
            _log_search_result(result, request)
            return result

        status = (
            EuropePMCSearchStatus.SUCCESS_WITH_RESULTS
            if records
            else EuropePMCSearchStatus.SUCCESS_EMPTY
        )
        result = _build_search_result(
            request=request,
            status=status,
            records=records,
            hit_count=hit_count,
            page_count=page_count,
            retry_count=retry_count,
            start_time=start_time,
        )
        _log_search_result(result, request)
        return result

    async def _request_page(
        self,
        *,
        client: httpx.AsyncClient,
        request: EuropePMCSearchRequest,
        cursor_mark: str,
        page_size: int,
    ) -> tuple[EuropePMCRawSearchPage, int]:
        form_data: dict[str, str | int] = {
            "query": request.query,
            "resultType": "core",
            "format": "json",
            "pageSize": page_size,
            "cursorMark": cursor_mark,
            "synonym": str(request.synonym).lower(),
        }
        if request.email:
            form_data["email"] = request.email

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post(
                    self.endpoint,
                    data=form_data,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                try:
                    response_data = response.json()
                except ValueError as exc:
                    raise _EuropePMCRequestFailure(
                        error_type="invalid_json",
                        message="Europe PMC 未返回有效 JSON",
                        retry_count=attempt,
                    ) from exc

                try:
                    page = EuropePMCRawSearchPage.model_validate(response_data)
                except ValidationError as exc:
                    raise _EuropePMCRequestFailure(
                        error_type="invalid_response_schema",
                        message="Europe PMC 响应结构校验失败",
                        retry_count=attempt,
                    ) from exc
                return page, attempt
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                retryable = status_code == 429 or status_code >= 500
                if retryable and attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _EuropePMCRequestFailure(
                    error_type="http_error",
                    message=f"Europe PMC 返回 HTTP {status_code}",
                    http_status=status_code,
                    retryable=retryable,
                    retry_count=attempt,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _EuropePMCRequestFailure(
                    error_type=type(exc).__name__,
                    message="Europe PMC 网络请求失败",
                    retryable=True,
                    retry_count=attempt,
                ) from exc
            except _EuropePMCRequestFailure:
                raise
            except Exception as exc:
                raise _EuropePMCRequestFailure(
                    error_type=type(exc).__name__,
                    message="Europe PMC 响应结构校验失败",
                    retry_count=attempt,
                ) from exc

        raise RuntimeError("unreachable")

    async def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        jitter = random.uniform(0, delay * 0.25) if delay else 0
        await asyncio.sleep(delay + jitter)


def _normalize_article(
    raw: dict[str, Any],
    *,
    search_run_id: str,
    retrieved_at: datetime,
) -> EuropePMCArticle:
    source_record_id = _clean_text(raw.get("id"))
    source_database_code = _clean_text(raw.get("source"))
    pmcid = _clean_text(raw.get("pmcid"))
    pmid = _clean_text(raw.get("pmid"))
    doi = _clean_text(raw.get("doi"))

    if not source_record_id:
        source_record_id = pmcid or pmid or doi
    if not source_record_id:
        raise ValueError("Europe PMC 文献记录缺少稳定标识符")

    article_id = f"europe_pmc:{source_database_code or 'unknown'}:{source_record_id}"
    authors = _normalize_authors(raw.get("authorList"))
    journal = _normalize_journal(raw)
    abstract_raw = _clean_text(raw.get("abstractText"))
    abstract = _html_to_plain_text(abstract_raw)
    resources = _normalize_full_text_resources(raw, article_id)
    preferred_resource = _select_preferred_resource(resources)

    in_europe_pmc = _optional_bool(raw.get("inEPMC"))
    in_pmc = _optional_bool(raw.get("inPMC"))
    source_has_pdf = _optional_bool(raw.get("hasPDF"))
    is_open_access = _optional_bool(raw.get("isOpenAccess"))
    has_full_text = bool(resources) or in_europe_pmc is True or in_pmc is True

    publication_date = (
        _clean_text(raw.get("firstPublicationDate"))
        or _clean_text(raw.get("electronicPublicationDate"))
        or journal["publication_date"]
    )

    return EuropePMCArticle(
        source_record_id=source_record_id,
        source_database_code=source_database_code,
        article_id=article_id,
        pmid=pmid,
        pmcid=pmcid,
        doi=doi,
        title=_clean_text(raw.get("title")),
        authors=authors,
        first_author=(
            authors[0].full_name
            if authors
            else _first_author_from_string(raw.get("authorString"))
        ),
        journal_title=journal["title"],
        journal_abbreviation=journal["abbreviation"],
        issn=journal["issn"],
        electronic_issn=journal["electronic_issn"],
        publication_date=publication_date,
        publication_year=_optional_int(raw.get("pubYear")),
        publication_types=_nested_string_list(raw.get("pubTypeList"), "pubType"),
        publication_status=_clean_text(raw.get("publicationStatus")),
        language=_clean_text(raw.get("language")),
        keywords=_nested_string_list(raw.get("keywordList"), "keyword"),
        abstract_raw=abstract_raw,
        abstract=abstract,
        abstract_available=abstract is not None,
        is_open_access=is_open_access,
        in_europe_pmc=in_europe_pmc,
        in_pmc=in_pmc,
        has_pdf=source_has_pdf,
        has_full_text=has_full_text,
        can_download_full_text=preferred_resource is not None,
        license=_clean_text(raw.get("license")),
        landing_url=_build_landing_url(pmcid, pmid, doi),
        full_text_resources=resources,
        preferred_full_text_format=(
            preferred_resource.format if preferred_resource else None
        ),
        preferred_download_url=(
            preferred_resource.url if preferred_resource else None
        ),
        cited_by_count=_optional_int(raw.get("citedByCount")),
        retrieved_at=retrieved_at,
        search_run_id=search_run_id,
    )


def _normalize_authors(value: Any) -> list[EuropePMCAuthor]:
    if not isinstance(value, dict) or not isinstance(value.get("author"), list):
        return []

    authors: list[EuropePMCAuthor] = []
    for raw_author in value["author"]:
        if not isinstance(raw_author, dict):
            continue
        full_name = _clean_text(raw_author.get("fullName"))
        if not full_name:
            continue

        author_id = raw_author.get("authorId")
        orcid = None
        if isinstance(author_id, dict):
            author_id_type = _clean_text(author_id.get("type"))
            if author_id_type and author_id_type.casefold() == "orcid":
                orcid = _clean_text(author_id.get("value"))

        authors.append(
            EuropePMCAuthor(
                full_name=full_name,
                first_name=_clean_text(raw_author.get("firstName")),
                last_name=_clean_text(raw_author.get("lastName")),
                initials=_clean_text(raw_author.get("initials")),
                orcid=orcid,
            )
        )
    return authors


def _normalize_journal(raw: dict[str, Any]) -> dict[str, str | None]:
    journal_info = raw.get("journalInfo")
    journal_info = journal_info if isinstance(journal_info, dict) else {}
    journal = journal_info.get("journal")
    journal = journal if isinstance(journal, dict) else {}

    return {
        "title": _clean_text(journal.get("title"))
        or _clean_text(raw.get("journalTitle")),
        "abbreviation": _clean_text(journal.get("ISOAbbreviation"))
        or _clean_text(journal.get("medlineAbbreviation")),
        "issn": _clean_text(journal.get("ISSN"))
        or _clean_text(raw.get("journalIssn")),
        "electronic_issn": _clean_text(journal.get("ESSN")),
        "publication_date": _clean_text(journal_info.get("printPublicationDate"))
        or _clean_text(journal_info.get("dateOfPublication")),
    }


def _normalize_full_text_resources(
    raw: dict[str, Any],
    article_id: str,
) -> list[FullTextResource]:
    container = raw.get("fullTextUrlList")
    if not isinstance(container, dict):
        return []
    raw_resources = container.get("fullTextUrl")
    if not isinstance(raw_resources, list):
        return []

    resources: list[FullTextResource] = []
    seen_urls: set[str] = set()
    for raw_resource in raw_resources:
        if not isinstance(raw_resource, dict):
            continue
        url = _clean_text(raw_resource.get("url"))
        original_style = _clean_text(raw_resource.get("documentStyle"))
        site = _clean_text(raw_resource.get("site"))
        if not url or not _is_http_url(url) or _is_landing_resource(original_style, site):
            continue
        if url.casefold() in seen_urls:
            continue
        seen_urls.add(url.casefold())

        format_ = _detect_full_text_format(original_style, url)
        availability = _clean_text(raw_resource.get("availability"))
        availability_code = _clean_text(raw_resource.get("availabilityCode"))
        open_access = _resource_open_access(availability, availability_code)
        downloadable = open_access if open_access is not None else None
        extension = _format_extension(format_)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        safe_article_id = article_id.replace(":", "_")

        resources.append(
            FullTextResource(
                resource_id=f"{article_id}:{format_.value}:{digest}",
                format=format_,
                original_style=original_style,
                url=url,
                site=site,
                availability=availability,
                availability_code=availability_code,
                is_open_access=open_access,
                is_downloadable=downloadable,
                suggested_file_name=f"{safe_article_id}_{digest}.{extension}",
            )
        )
    return resources


def _select_preferred_resource(
    resources: list[FullTextResource],
) -> FullTextResource | None:
    format_rank = {
        FullTextFormat.PDF: 0,
        FullTextFormat.EPUB: 1,
        FullTextFormat.HTML: 2,
        FullTextFormat.XML: 3,
        FullTextFormat.TEXT: 4,
        FullTextFormat.OTHER: 5,
    }
    downloadable = [item for item in resources if item.is_downloadable is True]
    if not downloadable:
        return None
    return min(downloadable, key=lambda item: format_rank[item.format])


def _detect_full_text_format(
    original_style: str | None,
    url: str,
) -> FullTextFormat:
    style = (original_style or "").casefold().strip()
    path = urlparse(url).path.casefold()
    candidates = f"{style} {path}"

    if "pdf" in candidates:
        return FullTextFormat.PDF
    if "epub" in candidates:
        return FullTextFormat.EPUB
    if "html" in candidates or "htm" in candidates:
        return FullTextFormat.HTML
    if "xml" in candidates:
        return FullTextFormat.XML
    if "text" in candidates or path.endswith(".txt"):
        return FullTextFormat.TEXT
    return FullTextFormat.OTHER


def _resource_open_access(
    availability: str | None,
    availability_code: str | None,
) -> bool | None:
    code = (availability_code or "").casefold().strip()
    label = (availability or "").casefold().strip()
    if code in {"oa", "f", "free"} or "open access" in label or label == "free":
        return True
    if code in {"s", "sub", "subscription"} or any(
        phrase in label for phrase in ("subscription", "restricted", "closed")
    ):
        return False
    return None


def _is_landing_resource(style: str | None, site: str | None) -> bool:
    return any(
        value and value.casefold().strip() == "doi"
        for value in (style, site)
    )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _format_extension(format_: FullTextFormat) -> str:
    return {
        FullTextFormat.PDF: "pdf",
        FullTextFormat.EPUB: "epub",
        FullTextFormat.HTML: "html",
        FullTextFormat.XML: "xml",
        FullTextFormat.TEXT: "txt",
        FullTextFormat.OTHER: "bin",
    }[format_]


def _html_to_plain_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _PlainTextHTMLParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def _nested_string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    items = value.get(key)
    if not isinstance(items, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = _clean_text(item)
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        result.append(cleaned)
    return result


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).casefold().strip()
    if normalized in {"y", "yes", "true", "1"}:
        return True
    if normalized in {"n", "no", "false", "0"}:
        return False
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _first_author_from_string(value: Any) -> str | None:
    author_string = _clean_text(value)
    if not author_string:
        return None
    return author_string.split(",", maxsplit=1)[0].strip() or None


def _build_landing_url(
    pmcid: str | None,
    pmid: str | None,
    doi: str | None,
) -> str | None:
    if pmcid:
        return f"https://europepmc.org/article/PMC/{pmcid}"
    if pmid:
        return f"https://europepmc.org/article/MED/{pmid}"
    if doi:
        return f"https://doi.org/{doi}"
    return None


def _build_search_result(
    *,
    request: EuropePMCSearchRequest,
    status: EuropePMCSearchStatus,
    records: list[EuropePMCArticle],
    hit_count: int | None,
    page_count: int,
    retry_count: int,
    start_time: float,
    error: EuropePMCSearchError | None = None,
) -> EuropePMCSearchResult:
    return EuropePMCSearchResult(
        search_run_id=request.search_run_id,
        status=status,
        hit_count=hit_count,
        retrieved_count=len(records),
        page_count=page_count,
        retry_count=retry_count,
        records=records,
        latency_ms=round((perf_counter() - start_time) * 1000, 1),
        error=error,
    )


def _log_search_result(
    result: EuropePMCSearchResult,
    request: EuropePMCSearchRequest,
) -> None:
    bound_logger = logger.bind(
        component="europe_pmc_searcher",
        event="europe_pmc_search_completed",
        search_run_id=result.search_run_id,
        status=result.status.value,
        query_length=len(request.query),
        page_size=request.page_size,
        page_count=result.page_count,
        hit_count=result.hit_count,
        retrieved_count=result.retrieved_count,
        abstract_present_count=sum(
            record.abstract_available for record in result.records
        ),
        open_access_count=sum(
            record.is_open_access is True for record in result.records
        ),
        downloadable_full_text_count=sum(
            record.can_download_full_text for record in result.records
        ),
        retry_count=result.retry_count,
        latency_ms=result.latency_ms,
        error_type=result.error.error_type if result.error else None,
        http_status=result.error.http_status if result.error else None,
    )
    if result.status in {
        EuropePMCSearchStatus.FAILED,
        EuropePMCSearchStatus.PARTIAL_SUCCESS,
    }:
        bound_logger.warning("Europe PMC 检索未完整完成")
    else:
        bound_logger.info("Europe PMC 检索完成")
