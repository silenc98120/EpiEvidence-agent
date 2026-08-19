"""多来源检索结果到统一证据记录的数据契约与适配器。"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from time import perf_counter
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from app.core.search_strategy_generator import SearchSource
from app.tools.article_search.europepmc import (
    EuropePMCArticle,
    EuropePMCSearchResult,
)
from app.tools.article_search.pmc import PMCArticle, PMCSearchResult
from app.tools.article_search.preprint import PreprintArticle, PreprintSearchResult
from app.tools.article_search.pubmed import PubMedArticle, PubMedSearchResult
from app.tools.article_search.semantic_scholar import (
    SemanticScholarArticle,
    SemanticScholarSearchResult,
)


class UnifiedSearchStatus(StrEnum):
    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class PeerReviewStatus(StrEnum):
    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    UNKNOWN = "unknown"


class UnifiedFullTextFormat(StrEnum):
    PDF = "pdf"
    EPUB = "epub"
    HTML = "html"
    XML = "xml"
    TEXT = "text"
    OTHER = "other"


class EvidenceAuthor(BaseModel):
    full_name: str = Field(min_length=1)
    first_name: str | None = None
    last_name: str | None = None
    initials: str | None = None
    collective_name: str | None = None
    orcid: str | None = None
    source_author_id: str | None = None


class FullTextResourceCandidate(BaseModel):
    source: SearchSource
    resource_id: str = Field(min_length=1)
    format: UnifiedFullTextFormat
    original_style: str | None = None
    url: str = Field(pattern=r"^https?://")
    site: str | None = None
    availability: str | None = None
    availability_code: str | None = None
    license: str | None = None
    is_open_access: bool | None = None
    is_downloadable: bool | None = None
    suggested_file_name: str | None = None


class EvidenceRecord(BaseModel):
    """尚未跨来源去重的一条统一文献记录。"""

    source: SearchSource
    source_record_id: str = Field(min_length=1)
    source_article_id: str = Field(min_length=1)
    source_rank: int | None = Field(default=None, ge=1)
    search_run_id: str = Field(min_length=1)

    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str = Field(min_length=1)
    normalized_title: str = Field(min_length=1)
    abstract: str | None = None
    abstract_available: bool = False
    authors: list[EvidenceAuthor] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    journal_abbreviation: str | None = None
    issn: str | None = None
    electronic_issn: str | None = None
    publication_date: str | None = None
    publication_year: int | None = Field(default=None, ge=1500, le=3000)
    publication_types: list[str] = Field(default_factory=list)
    study_design: str | None = None
    publication_status: str | None = None
    language: str | None = None
    keywords: list[str] = Field(default_factory=list)

    peer_review_status: PeerReviewStatus = PeerReviewStatus.UNKNOWN
    preprint_server: str | None = None
    preprint_version: int | None = Field(default=None, ge=1)
    published_doi: str | None = None
    is_open_access: bool | None = None
    has_full_text: bool = False
    can_download_full_text: bool = False
    license: str | None = None
    full_text_resources: list[FullTextResourceCandidate] = Field(default_factory=list)
    citation_count: int | None = Field(default=None, ge=0)
    is_retracted: bool = False
    landing_url: str | None = None
    retrieved_at: datetime

    @model_validator(mode="after")
    def validate_full_text_claims(self) -> "EvidenceRecord":
        if self.can_download_full_text and not any(
            resource.is_downloadable is True for resource in self.full_text_resources
        ):
            raise ValueError("可下载全文必须至少包含一个明确可下载的资源")
        if self.preprint_server and self.peer_review_status != PeerReviewStatus.PREPRINT:
            raise ValueError("存在 preprint_server 时必须标记为 preprint")
        return self


class UnifiedSearchError(BaseModel):
    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool = False


class UnifiedSearchResult(BaseModel):
    source: SearchSource
    search_run_id: str
    status: UnifiedSearchStatus
    hit_count: int | None = Field(default=None, ge=0)
    retrieved_count: int = Field(default=0, ge=0)
    page_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    invalid_record_count: int = Field(default=0, ge=0)
    records: list[EvidenceRecord] = Field(default_factory=list)
    latency_ms: float = Field(ge=0.0)
    normalization_latency_ms: float = Field(ge=0.0)
    error: UnifiedSearchError | None = None


ProviderArticle = (
    PubMedArticle
    | EuropePMCArticle
    | SemanticScholarArticle
    | PMCArticle
    | PreprintArticle
)
ProviderSearchResult = (
    PubMedSearchResult
    | EuropePMCSearchResult
    | SemanticScholarSearchResult
    | PMCSearchResult
    | PreprintSearchResult
)


class SearchResultsNormalizer:
    """将平台专属文章或检索结果转换为统一数据契约。"""

    def normalize_article(
        self,
        article: ProviderArticle,
        *,
        source_rank: int | None = None,
    ) -> EvidenceRecord:
        if isinstance(article, PubMedArticle):
            return _from_pubmed(article, source_rank)
        if isinstance(article, EuropePMCArticle):
            return _from_europe_pmc(article, source_rank)
        if isinstance(article, SemanticScholarArticle):
            return _from_semantic_scholar(article, source_rank)
        if isinstance(article, PMCArticle):
            return _from_pmc(article, source_rank)
        if isinstance(article, PreprintArticle):
            return _from_preprint(article, source_rank)
        raise TypeError(f"不支持的文章模型: {type(article).__name__}")

    def normalize_result(
        self,
        result: ProviderSearchResult,
    ) -> UnifiedSearchResult:
        start_time = perf_counter()
        source = _result_source(result)
        records: list[EvidenceRecord] = []
        newly_invalid = 0
        for rank, article in enumerate(result.records, start=1):
            try:
                records.append(self.normalize_article(article, source_rank=rank))
            except Exception as exc:
                newly_invalid += 1
                logger.bind(
                    component="search_results_normalizer",
                    event="search_record_normalization_failed",
                    source=source.value,
                    search_run_id=result.search_run_id,
                    source_rank=rank,
                    error_type=type(exc).__name__,
                ).warning("单条文献标准化失败")

        original_invalid = int(getattr(result, "invalid_record_count", 0) or 0)
        invalid_record_count = original_invalid + newly_invalid
        status = UnifiedSearchStatus(result.status.value)
        if newly_invalid and status != UnifiedSearchStatus.FAILED:
            status = UnifiedSearchStatus.PARTIAL_SUCCESS
        normalization_latency_ms = (perf_counter() - start_time) * 1000
        unified = UnifiedSearchResult(
            source=source,
            search_run_id=result.search_run_id,
            status=status,
            hit_count=_result_hit_count(result),
            retrieved_count=len(records),
            page_count=int(getattr(result, "page_count", 1) or 0),
            retry_count=int(getattr(result, "retry_count", 0) or 0),
            invalid_record_count=invalid_record_count,
            records=records,
            latency_ms=float(result.latency_ms),
            normalization_latency_ms=normalization_latency_ms,
            error=_normalize_error(getattr(result, "error", None)),
        )
        logger.bind(
            component="search_results_normalizer",
            event="search_result_normalization_completed",
            source=source.value,
            search_run_id=result.search_run_id,
            input_count=len(result.records),
            output_count=len(records),
            invalid_record_count=invalid_record_count,
            normalization_latency_ms=round(normalization_latency_ms, 1),
        ).info("检索结果标准化完成")
        return unified


def _base_record(
    *,
    source: SearchSource,
    source_record_id: str,
    source_article_id: str,
    source_rank: int | None,
    search_run_id: str,
    doi: str | None,
    pmid: str | None,
    pmcid: str | None,
    title: str | None,
    abstract: str | None,
    authors: list[EvidenceAuthor],
    first_author: str | None,
    journal_title: str | None,
    journal_abbreviation: str | None,
    issn: str | None,
    electronic_issn: str | None,
    publication_date: str | None,
    publication_year: int | None,
    publication_types: list[str],
    study_design: str | None,
    publication_status: str | None,
    language: str | None,
    keywords: list[str],
    peer_review_status: PeerReviewStatus,
    preprint_server: str | None,
    preprint_version: int | None,
    published_doi: str | None,
    is_open_access: bool | None,
    has_full_text: bool,
    can_download_full_text: bool,
    license_name: str | None,
    full_text_resources: list[FullTextResourceCandidate],
    citation_count: int | None,
    landing_url: str | None,
    retrieved_at: datetime,
) -> EvidenceRecord:
    cleaned_title = _single_line(title)
    if not cleaned_title:
        raise ValueError("文献缺少题名")
    cleaned_abstract = _multiline_text(abstract)
    return EvidenceRecord(
        source=source,
        source_record_id=_required_text(source_record_id, "source_record_id"),
        source_article_id=_required_text(source_article_id, "source_article_id"),
        source_rank=source_rank,
        search_run_id=_required_text(search_run_id, "search_run_id"),
        doi=normalize_doi(doi),
        pmid=normalize_pmid(pmid),
        pmcid=normalize_pmcid(pmcid),
        title=cleaned_title,
        normalized_title=normalize_title(cleaned_title),
        abstract=cleaned_abstract,
        abstract_available=cleaned_abstract is not None,
        authors=authors,
        first_author=_single_line(first_author),
        journal_title=_single_line(journal_title),
        journal_abbreviation=_single_line(journal_abbreviation),
        issn=_single_line(issn),
        electronic_issn=_single_line(electronic_issn),
        publication_date=_single_line(publication_date),
        publication_year=publication_year,
        publication_types=_deduplicate_texts(publication_types),
        study_design=_single_line(study_design),
        publication_status=_single_line(publication_status),
        language=_single_line(language),
        keywords=_deduplicate_texts(keywords),
        peer_review_status=peer_review_status,
        preprint_server=preprint_server,
        preprint_version=preprint_version,
        published_doi=normalize_doi(published_doi),
        is_open_access=is_open_access,
        has_full_text=has_full_text,
        can_download_full_text=can_download_full_text,
        license=_single_line(license_name),
        full_text_resources=full_text_resources,
        citation_count=citation_count,
        landing_url=_http_url(landing_url),
        retrieved_at=retrieved_at,
    )


def _from_pubmed(article: PubMedArticle, rank: int | None) -> EvidenceRecord:
    authors = [
        EvidenceAuthor(
            full_name=author.full_name,
            first_name=author.first_name,
            last_name=author.last_name,
            initials=author.initials,
            collective_name=author.collective_name,
        )
        for author in article.authors
    ]
    return _base_record(
        source=SearchSource.PUBMED,
        source_record_id=article.source_record_id,
        source_article_id=article.article_id,
        source_rank=rank,
        search_run_id=article.search_run_id,
        doi=article.doi,
        pmid=article.pmid,
        pmcid=article.pmcid,
        title=article.title,
        abstract=article.abstract,
        authors=authors,
        first_author=article.first_author,
        journal_title=article.journal_title,
        journal_abbreviation=article.journal_abbreviation,
        issn=article.issn,
        electronic_issn=article.electronic_issn,
        publication_date=article.publication_date,
        publication_year=article.publication_year,
        publication_types=article.publication_types,
        study_design=None,
        publication_status=article.publication_status,
        language=article.language,
        keywords=article.keywords,
        peer_review_status=PeerReviewStatus.UNKNOWN,
        preprint_server=None,
        preprint_version=None,
        published_doi=None,
        is_open_access=None,
        has_full_text=article.has_full_text,
        can_download_full_text=False,
        license_name=None,
        full_text_resources=[],
        citation_count=None,
        landing_url=article.landing_url,
        retrieved_at=article.retrieved_at,
    )


def _from_europe_pmc(
    article: EuropePMCArticle,
    rank: int | None,
) -> EvidenceRecord:
    authors = [
        EvidenceAuthor(
            full_name=author.full_name,
            first_name=author.first_name,
            last_name=author.last_name,
            initials=author.initials,
            orcid=author.orcid,
        )
        for author in article.authors
    ]
    resources = [
        FullTextResourceCandidate(
            source=SearchSource.EUROPE_PMC,
            resource_id=resource.resource_id,
            format=_unified_format(resource.format.value),
            original_style=resource.original_style,
            url=resource.url,
            site=resource.site,
            availability=resource.availability,
            availability_code=resource.availability_code,
            is_open_access=resource.is_open_access,
            is_downloadable=resource.is_downloadable,
            suggested_file_name=resource.suggested_file_name,
        )
        for resource in article.full_text_resources
    ]
    return _base_record(
        source=SearchSource.EUROPE_PMC,
        source_record_id=article.source_record_id,
        source_article_id=article.article_id,
        source_rank=rank,
        search_run_id=article.search_run_id,
        doi=article.doi,
        pmid=article.pmid,
        pmcid=article.pmcid,
        title=article.title,
        abstract=article.abstract,
        authors=authors,
        first_author=article.first_author,
        journal_title=article.journal_title,
        journal_abbreviation=article.journal_abbreviation,
        issn=article.issn,
        electronic_issn=article.electronic_issn,
        publication_date=article.publication_date,
        publication_year=article.publication_year,
        publication_types=article.publication_types,
        study_design=article.study_design,
        publication_status=article.publication_status,
        language=article.language,
        keywords=article.keywords,
        peer_review_status=PeerReviewStatus.UNKNOWN,
        preprint_server=None,
        preprint_version=None,
        published_doi=None,
        is_open_access=article.is_open_access,
        has_full_text=article.has_full_text,
        can_download_full_text=article.can_download_full_text,
        license_name=article.license,
        full_text_resources=resources,
        citation_count=article.cited_by_count,
        landing_url=article.landing_url,
        retrieved_at=article.retrieved_at,
    )


def _from_semantic_scholar(
    article: SemanticScholarArticle,
    rank: int | None,
) -> EvidenceRecord:
    authors = [
        EvidenceAuthor(
            full_name=author.name,
            source_author_id=author.author_id,
        )
        for author in article.authors
    ]
    resources = [
        FullTextResourceCandidate(
            source=SearchSource.SEMANTIC_SCHOLAR,
            resource_id=resource.resource_id,
            format=UnifiedFullTextFormat.PDF,
            original_style=resource.format,
            url=resource.url,
            availability=resource.status,
            license=resource.license,
            is_open_access=resource.is_open_access,
            is_downloadable=resource.is_downloadable,
            suggested_file_name=resource.suggested_file_name,
        )
        for resource in article.full_text_resources
    ]
    return _base_record(
        source=SearchSource.SEMANTIC_SCHOLAR,
        source_record_id=article.source_record_id,
        source_article_id=article.article_id,
        source_rank=rank,
        search_run_id=article.search_run_id,
        doi=article.doi,
        pmid=article.pmid,
        pmcid=article.pmcid,
        title=article.title,
        abstract=article.abstract,
        authors=authors,
        first_author=article.first_author,
        journal_title=article.journal_title,
        journal_abbreviation=None,
        issn=None,
        electronic_issn=None,
        publication_date=article.publication_date,
        publication_year=article.publication_year,
        publication_types=article.publication_types,
        study_design=None,
        publication_status=None,
        language=None,
        keywords=article.fields_of_study,
        peer_review_status=PeerReviewStatus.UNKNOWN,
        preprint_server=None,
        preprint_version=None,
        published_doi=None,
        is_open_access=article.is_open_access,
        has_full_text=article.has_full_text,
        can_download_full_text=bool(resources),
        license_name=resources[0].license if resources else None,
        full_text_resources=resources,
        citation_count=article.citation_count,
        landing_url=article.landing_url,
        retrieved_at=article.retrieved_at,
    )


def _from_pmc(article: PMCArticle, rank: int | None) -> EvidenceRecord:
    authors = [
        EvidenceAuthor(
            full_name=author.full_name,
            first_name=author.first_name,
            last_name=author.last_name,
            collective_name=author.collective_name,
        )
        for author in article.authors
    ]
    return _base_record(
        source=SearchSource.PMC,
        source_record_id=article.source_record_id,
        source_article_id=article.article_id,
        source_rank=rank,
        search_run_id=article.search_run_id,
        doi=article.doi,
        pmid=article.pmid,
        pmcid=article.pmcid,
        title=article.title,
        abstract=article.abstract,
        authors=authors,
        first_author=article.first_author,
        journal_title=article.journal_title,
        journal_abbreviation=None,
        issn=None,
        electronic_issn=None,
        publication_date=article.publication_date,
        publication_year=article.publication_year,
        publication_types=article.publication_types,
        study_design=None,
        publication_status=None,
        language=None,
        keywords=[],
        peer_review_status=PeerReviewStatus.UNKNOWN,
        preprint_server=None,
        preprint_version=None,
        published_doi=None,
        is_open_access=article.is_open_access,
        has_full_text=article.has_full_text,
        can_download_full_text=article.can_download_full_text,
        license_name=article.license,
        full_text_resources=[],
        citation_count=None,
        landing_url=article.landing_url,
        retrieved_at=article.retrieved_at,
    )


def _from_preprint(article: PreprintArticle, rank: int | None) -> EvidenceRecord:
    source = (
        SearchSource.BIORXIV
        if article.preprint_server.value == SearchSource.BIORXIV.value
        else SearchSource.MEDRXIV
    )
    resources: list[FullTextResourceCandidate] = []
    if article.jatsxml_url:
        resources.append(
            FullTextResourceCandidate(
                source=source,
                resource_id=f"{article.article_id}:xml",
                format=UnifiedFullTextFormat.XML,
                original_style="jatsxml",
                url=article.jatsxml_url,
                license=article.license,
                is_open_access=None,
                is_downloadable=True,
                suggested_file_name=(
                    f"{article.preprint_server.value}_"
                    f"{re.sub(r'[^A-Za-z0-9._-]+', '_', article.doi)}.xml"
                ),
            )
        )
    return _base_record(
        source=source,
        source_record_id=article.source_record_id,
        source_article_id=article.article_id,
        source_rank=rank,
        search_run_id=article.search_run_id,
        doi=article.doi,
        pmid=None,
        pmcid=None,
        title=article.title,
        abstract=article.abstract,
        authors=[EvidenceAuthor(full_name=name) for name in article.authors],
        first_author=article.first_author,
        journal_title=article.preprint_server.value,
        journal_abbreviation=None,
        issn=None,
        electronic_issn=None,
        publication_date=article.publication_date,
        publication_year=article.publication_year,
        publication_types=[article.publication_type] if article.publication_type else [],
        study_design=None,
        publication_status="preprint",
        language=None,
        keywords=[article.category] if article.category else [],
        peer_review_status=PeerReviewStatus.PREPRINT,
        preprint_server=article.preprint_server.value,
        preprint_version=article.version,
        published_doi=article.published_doi,
        is_open_access=None,
        has_full_text=article.has_full_text,
        can_download_full_text=bool(resources),
        license_name=article.license,
        full_text_resources=resources,
        citation_count=None,
        landing_url=article.landing_url,
        retrieved_at=article.retrieved_at,
    )


def _result_source(result: ProviderSearchResult) -> SearchSource:
    if isinstance(result, PubMedSearchResult):
        return SearchSource.PUBMED
    if isinstance(result, EuropePMCSearchResult):
        return SearchSource.EUROPE_PMC
    if isinstance(result, SemanticScholarSearchResult):
        return SearchSource.SEMANTIC_SCHOLAR
    if isinstance(result, PMCSearchResult):
        return SearchSource.PMC
    if isinstance(result, PreprintSearchResult):
        return SearchSource(result.source)
    raise TypeError(f"不支持的检索结果模型: {type(result).__name__}")


def _result_hit_count(result: ProviderSearchResult) -> int | None:
    if isinstance(result, PreprintSearchResult):
        return result.requested_count
    return getattr(result, "hit_count", None)


def _normalize_error(error: Any) -> UnifiedSearchError | None:
    if error is None:
        return None
    return UnifiedSearchError(
        error_type=str(error.error_type),
        message=str(error.message),
        http_status=getattr(error, "http_status", None),
        retryable=bool(getattr(error, "retryable", False)),
    )


def normalize_doi(value: str | None) -> str | None:
    cleaned = _single_line(value)
    if not cleaned:
        return None
    cleaned = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^doi\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().casefold()
    return cleaned if cleaned.startswith("10.") and "/" in cleaned else None


def normalize_pmid(value: str | None) -> str | None:
    cleaned = _single_line(value)
    return cleaned if cleaned and cleaned.isdigit() else None


def normalize_pmcid(value: str | None) -> str | None:
    cleaned = _single_line(value)
    if not cleaned:
        return None
    cleaned = cleaned.upper()
    if cleaned.isdigit():
        cleaned = f"PMC{cleaned}"
    return cleaned if re.fullmatch(r"PMC\d+", cleaned) else None


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError("题名标准化后不能为空")
    return normalized


def _unified_format(value: str | None) -> UnifiedFullTextFormat:
    normalized = (value or "").casefold()
    try:
        return UnifiedFullTextFormat(normalized)
    except ValueError:
        return UnifiedFullTextFormat.OTHER


def _required_text(value: str | None, field_name: str) -> str:
    cleaned = _single_line(value)
    if not cleaned:
        raise ValueError(f"{field_name} 不能为空")
    return cleaned


def _single_line(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().split())
    return cleaned or None


def _multiline_text(value: Any) -> str | None:
    if value is None:
        return None
    lines = [" ".join(line.split()) for line in str(value).splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or None


def _deduplicate_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _single_line(value)
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            result.append(cleaned)
    return result


def _http_url(value: str | None) -> str | None:
    cleaned = _single_line(value)
    if cleaned and re.match(r"^https?://", cleaned, flags=re.IGNORECASE):
        return cleaned
    return None
