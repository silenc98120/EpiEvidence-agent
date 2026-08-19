"""异步 PubMed 检索工具。

第一版只负责 ESearch + EFetch 的摘要检索和基础元数据标准化。全文下载由后续
的全文资源工具负责，避免把 PubMed 的 PMCID 误判为已经可下载的 OA 文件。
"""

from __future__ import annotations

import asyncio
import random
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import BaseModel, Field, model_validator


PUBMED_ESEARCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_EFETCH_ENDPOINT = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class PubMedSearchStatus(StrEnum):
    """一次 PubMed 检索的可观测结束状态。"""

    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class PubMedSearchRequest(BaseModel):
    """PubMed 检索请求；query 必须已经由 PubMed compiler 生成。"""

    query: str = Field(min_length=1)
    page_size: int = Field(default=100, ge=1, le=100)
    max_results: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=100)
    email: str | None = None
    api_key: str | None = None
    tool: str = Field(default="epi-research-agent", min_length=1, max_length=100)
    search_run_id: str = Field(default_factory=lambda: str(uuid4()))

    @model_validator(mode="after")
    def normalize_text_fields(self) -> "PubMedSearchRequest":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("PubMed 检索式不能为空")
        self.email = self.email.strip() or None if self.email else None
        self.api_key = self.api_key.strip() or None if self.api_key else None
        self.tool = self.tool.strip()
        return self


class PubMedAuthor(BaseModel):
    """PubMed 作者基础信息。"""

    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    initials: str | None = None
    collective_name: str | None = None


class PubMedArticle(BaseModel):
    """供后续统一化和摘要筛选使用的 PubMed 文献记录。"""

    source: str = "pubmed"
    source_record_id: str
    article_id: str
    pmid: str
    pmcid: str | None = None
    doi: str | None = None

    title: str | None = None
    authors: list[PubMedAuthor] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    journal_abbreviation: str | None = None
    issn: str | None = None
    electronic_issn: str | None = None
    publication_date: str | None = None
    publication_year: int | None = None
    publication_types: list[str] = Field(default_factory=list)
    publication_status: str | None = None
    language: str | None = None
    keywords: list[str] = Field(default_factory=list)

    abstract_raw: str | None = None
    abstract: str | None = None
    abstract_available: bool = False

    # PubMed EFetch does not prove that a direct, downloadable full-text file exists.
    has_full_text: bool = False
    landing_url: str
    retrieved_at: datetime
    search_run_id: str


class PubMedSearchError(BaseModel):
    """不包含原始医学检索式或上游响应正文的错误摘要。"""

    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class PubMedSearchResult(BaseModel):
    """一次有界 PubMed 检索的完整结果。"""

    source: str = "pubmed"
    search_run_id: str
    status: PubMedSearchStatus
    hit_count: int | None = None
    retrieved_count: int = 0
    page_count: int = 0
    retry_count: int = 0
    records: list[PubMedArticle] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    error: PubMedSearchError | None = None


class _PubMedRequestFailure(Exception):
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
        self.error = PubMedSearchError(
            error_type=error_type,
            message=message,
            http_status=http_status,
            retryable=retryable,
        )
        self.retry_count = retry_count


class PubMedSearcher:
    """通过 NCBI E-utilities 搜索 PubMed 并获取摘要。"""

    def __init__(
        self,
        *,
        esearch_endpoint: str = PUBMED_ESEARCH_ENDPOINT,
        efetch_endpoint: str = PUBMED_EFETCH_ENDPOINT,
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

        self.esearch_endpoint = esearch_endpoint
        self.efetch_endpoint = efetch_endpoint
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._client = client

    async def search(self, request: PubMedSearchRequest) -> PubMedSearchResult:
        """执行有界分页检索，不下载全文文件。"""

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
        request: PubMedSearchRequest,
        client: httpx.AsyncClient,
        start_time: float,
    ) -> PubMedSearchResult:
        records: list[PubMedArticle] = []
        offset = 0
        page_count = 0
        retry_count = 0
        hit_count: int | None = None

        try:
            while len(records) < request.max_results and page_count < request.max_pages:
                remaining = request.max_results - len(records)
                page_size = min(request.page_size, remaining)
                ids, page_hit_count, search_retries = await self._esearch(
                    client=client,
                    request=request,
                    retstart=offset,
                    retmax=page_size,
                )
                retry_count += search_retries
                page_count += 1
                hit_count = page_hit_count

                if not ids:
                    break

                page_records, fetch_retries = await self._efetch(
                    client=client,
                    request=request,
                    pmids=ids,
                )
                retry_count += fetch_retries
                records.extend(page_records[:remaining])
                offset += len(ids)

                if offset >= hit_count or len(ids) < page_size:
                    break
        except _PubMedRequestFailure as exc:
            retry_count += exc.retry_count
            result = self._build_result(
                request=request,
                status=(
                    PubMedSearchStatus.PARTIAL_SUCCESS
                    if records
                    else PubMedSearchStatus.FAILED
                ),
                records=records,
                hit_count=hit_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=exc.error,
            )
            self._log_result(result)
            return result
        except Exception as exc:
            logger.bind(
                component="pubmed_searcher",
                event="pubmed_search_unexpected_error",
                search_run_id=request.search_run_id,
                query_length=len(request.query),
                error_type=type(exc).__name__,
            ).exception("PubMed 检索发生未预期错误")
            result = self._build_result(
                request=request,
                status=(
                    PubMedSearchStatus.PARTIAL_SUCCESS
                    if records
                    else PubMedSearchStatus.FAILED
                ),
                records=records,
                hit_count=hit_count,
                page_count=page_count,
                retry_count=retry_count,
                start_time=start_time,
                error=PubMedSearchError(
                    error_type=type(exc).__name__,
                    message="PubMed 响应处理失败",
                ),
            )
            self._log_result(result)
            return result

        result = self._build_result(
            request=request,
            status=(
                PubMedSearchStatus.SUCCESS_WITH_RESULTS
                if records
                else PubMedSearchStatus.SUCCESS_EMPTY
            ),
            records=records,
            hit_count=hit_count,
            page_count=page_count,
            retry_count=retry_count,
            start_time=start_time,
        )
        self._log_result(result)
        return result

    async def _esearch(
        self,
        *,
        client: httpx.AsyncClient,
        request: PubMedSearchRequest,
        retstart: int,
        retmax: int,
    ) -> tuple[list[str], int, int]:
        params = self._common_params(request)
        params.update(
            {
                "db": "pubmed",
                "term": request.query,
                "retmode": "json",
                "retstart": str(retstart),
                "retmax": str(retmax),
                "sort": "relevance",
            }
        )
        response, retries = await self._request(
            client=client,
            endpoint=self.esearch_endpoint,
            params=params,
        )
        try:
            payload = response.json()
            result = payload["esearchresult"]
            ids = [str(value) for value in result.get("idlist", [])]
            hit_count = int(result.get("count", 0))
        except (ValueError, KeyError, TypeError) as exc:
            raise _PubMedRequestFailure(
                error_type="invalid_json",
                message="PubMed ESearch 未返回有效 JSON",
                retry_count=retries,
            ) from exc
        return ids, hit_count, retries

    async def _efetch(
        self,
        *,
        client: httpx.AsyncClient,
        request: PubMedSearchRequest,
        pmids: list[str],
    ) -> tuple[list[PubMedArticle], int]:
        params = self._common_params(request)
        params.update(
            {
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml",
            }
        )
        response, retries = await self._request(
            client=client,
            endpoint=self.efetch_endpoint,
            params=params,
        )
        try:
            root = ET.fromstring(response.content)
            retrieved_at = datetime.now(UTC)
            records = [
                _normalize_article(
                    article,
                    search_run_id=request.search_run_id,
                    retrieved_at=retrieved_at,
                )
                for article in _children(root, "PubmedArticle")
            ]
        except (ET.ParseError, ValueError) as exc:
            raise _PubMedRequestFailure(
                error_type="invalid_xml",
                message="PubMed EFetch 未返回有效 XML",
                retry_count=retries,
            ) from exc
        return records, retries

    async def _request(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        params: dict[str, str],
    ) -> tuple[httpx.Response, int]:
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.get(
                    endpoint,
                    params=params,
                    headers={"Accept": "application/json, application/xml"},
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        await self._wait_before_retry(attempt)
                        continue
                    raise _PubMedRequestFailure(
                        error_type="http_error",
                        message=f"PubMed 返回 HTTP {response.status_code}",
                        http_status=response.status_code,
                        retryable=True,
                        retry_count=attempt,
                    )
                response.raise_for_status()
                return response, attempt
            except _PubMedRequestFailure:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                    continue
                raise _PubMedRequestFailure(
                    error_type=type(exc).__name__,
                    message="PubMed 网络请求失败",
                    retryable=True,
                    retry_count=attempt,
                ) from exc
            except httpx.HTTPStatusError as exc:
                raise _PubMedRequestFailure(
                    error_type="http_error",
                    message=f"PubMed 返回 HTTP {exc.response.status_code}",
                    http_status=exc.response.status_code,
                    retryable=False,
                    retry_count=attempt,
                ) from exc

        raise RuntimeError("unreachable")

    @staticmethod
    def _common_params(request: PubMedSearchRequest) -> dict[str, str]:
        params = {"tool": request.tool}
        if request.email:
            params["email"] = request.email
        if request.api_key:
            params["api_key"] = request.api_key
        return params

    async def _wait_before_retry(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        jitter = random.uniform(0, delay * 0.25) if delay else 0
        await asyncio.sleep(delay + jitter)

    @staticmethod
    def _build_result(
        *,
        request: PubMedSearchRequest,
        status: PubMedSearchStatus,
        records: list[PubMedArticle],
        hit_count: int | None,
        page_count: int,
        retry_count: int,
        start_time: float,
        error: PubMedSearchError | None = None,
    ) -> PubMedSearchResult:
        return PubMedSearchResult(
            search_run_id=request.search_run_id,
            status=status,
            hit_count=hit_count,
            retrieved_count=len(records),
            page_count=page_count,
            retry_count=retry_count,
            records=records,
            latency_ms=(perf_counter() - start_time) * 1000,
            error=error,
        )

    @staticmethod
    def _log_result(result: PubMedSearchResult) -> None:
        logger.bind(
            component="pubmed_searcher",
            event="pubmed_search_completed",
            search_run_id=result.search_run_id,
            status=result.status.value,
            hit_count=result.hit_count,
            retrieved_count=result.retrieved_count,
            page_count=result.page_count,
            retry_count=result.retry_count,
            latency_ms=round(result.latency_ms, 1),
        ).info("PubMed 检索完成")


def _normalize_article(
    article: ET.Element,
    *,
    search_run_id: str,
    retrieved_at: datetime,
) -> PubMedArticle:
    medline = _first_descendant(article, "MedlineCitation")
    if medline is None:
        medline = article
    pubmed_data = _first_descendant(article, "PubmedData")
    if pubmed_data is None:
        pubmed_data = article
    pmid = _first_text(medline, "PMID")
    if not pmid:
        raise ValueError("PubMed 文献记录缺少 PMID")

    article_node = _first_descendant(medline, "Article")
    if article_node is None:
        article_node = medline
    title = _first_text(article_node, "ArticleTitle")
    journal = _first_descendant(article_node, "Journal")
    journal_issue = _first_descendant(article_node, "JournalIssue")
    publication_date, publication_year = _publication_date(article_node, journal_issue)
    issn, electronic_issn = (
        _journal_issns(journal) if journal is not None else (None, None)
    )

    abstract_node = _first_descendant(article_node, "Abstract")
    abstract = _abstract_text(abstract_node)
    ids = {
        (node.attrib.get("IdType") or "").casefold(): _node_text(node)
        for node in _descendants(pubmed_data, "ArticleId")
        if _node_text(node)
    }
    authors = _authors(article_node)
    pmcid = ids.get("pmc")
    return PubMedArticle(
        source_record_id=pmid,
        article_id=f"pubmed:pmid:{pmid}",
        pmid=pmid,
        pmcid=pmcid,
        doi=ids.get("doi"),
        title=title,
        authors=authors,
        first_author=authors[0].full_name if authors else None,
        journal_title=_first_text(journal, "Title") if journal is not None else None,
        journal_abbreviation=(
            _first_text(journal, "ISOAbbreviation") if journal is not None else None
        ),
        issn=issn,
        electronic_issn=electronic_issn,
        publication_date=publication_date,
        publication_year=publication_year,
        publication_types=[
            _node_text(node)
            for node in _descendants(article_node, "PublicationType")
            if _node_text(node)
        ],
        publication_status=_first_text(pubmed_data, "PublicationStatus"),
        language=_first_text(article_node, "Language"),
        keywords=[
            _node_text(node)
            for node in _descendants(article_node, "Keyword")
            if _node_text(node)
        ],
        abstract_raw=_node_text(abstract_node) if abstract_node is not None else None,
        abstract=abstract,
        abstract_available=abstract is not None,
        has_full_text=False,
        landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        retrieved_at=retrieved_at,
        search_run_id=search_run_id,
    )


def _authors(article: ET.Element) -> list[PubMedAuthor]:
    author_list = _first_descendant(article, "AuthorList")
    if author_list is None:
        return []
    authors: list[PubMedAuthor] = []
    for author_node in _children(author_list, "Author"):
        collective = _first_text(author_node, "CollectiveName")
        last_name = _first_text(author_node, "LastName")
        first_name = _first_text(author_node, "ForeName")
        initials = _first_text(author_node, "Initials")
        full_name = collective or " ".join(
            part for part in (first_name, last_name) if part
        )
        if not full_name:
            full_name = initials
        if not full_name:
            continue
        authors.append(
            PubMedAuthor(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                initials=initials,
                collective_name=collective,
            )
        )
    return authors


def _publication_date(
    article: ET.Element,
    journal_issue: ET.Element | None,
) -> tuple[str | None, int | None]:
    date_node = _first_descendant(article, "ArticleDate")
    if date_node is None and journal_issue is not None:
        date_node = _first_descendant(journal_issue, "PubDate")
    if date_node is None:
        return None, None

    year_text = _first_text(date_node, "Year")
    month_text = _first_text(date_node, "Month")
    day_text = _first_text(date_node, "Day")
    if not year_text:
        match = re.search(r"\b(\d{4})\b", _node_text(date_node))
        year_text = match.group(1) if match else None
    if not year_text or not year_text.isdigit():
        return _node_text(date_node) or None, None

    year = int(year_text)
    month = _month_number(month_text)
    if month and day_text and day_text.isdigit():
        return f"{year:04d}-{month:02d}-{int(day_text):02d}", year
    if month:
        return f"{year:04d}-{month:02d}", year
    return str(year), year


def _month_number(value: str | None) -> int | None:
    if not value:
        return None
    if value.isdigit() and 1 <= int(value) <= 12:
        return int(value)
    names = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return names.get(value[:3].casefold())


def _abstract_text(abstract_node: ET.Element | None) -> str | None:
    if abstract_node is None:
        return None
    sections: list[str] = []
    children = _children(abstract_node, "AbstractText")
    if not children:
        value = _node_text(abstract_node)
        return value or None
    for section in children:
        value = _node_text(section)
        if not value:
            continue
        label = section.attrib.get("Label") or section.attrib.get("NlmCategory")
        sections.append(f"{label}: {value}" if label else value)
    return "\n".join(sections) or None


def _journal_issns(journal: ET.Element) -> tuple[str | None, str | None]:
    values: dict[str, str] = {}
    for node in _descendants(journal, "ISSN"):
        value = _node_text(node)
        if value:
            values[(node.attrib.get("IssnType") or "unknown").casefold()] = value
    fallback = _first_text(journal, "ISSN")
    return values.get("print") or fallback, values.get("electronic")


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [node for node in element.iter() if _local_name(node.tag) == name]


def _first_descendant(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None
    return next(iter(_descendants(element, name)), None)


def _first_text(element: ET.Element | None, name: str) -> str | None:
    node = _first_descendant(element, name)
    return _node_text(node) if node is not None else None


def _node_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
