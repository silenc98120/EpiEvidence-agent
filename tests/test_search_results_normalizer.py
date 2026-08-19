from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.search_results_normalizer import (
    PeerReviewStatus,
    SearchResultsNormalizer,
    UnifiedFullTextFormat,
    UnifiedSearchStatus,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
)
from app.core.search_strategy_generator import SearchSource
from app.tools.article_search.europepmc import (
    EuropePMCArticle,
    EuropePMCAuthor,
    FullTextFormat,
    FullTextResource,
)
from app.tools.article_search.pmc import PMCArticle, PMCAuthor
from app.tools.article_search.preprint import PreprintArticle, PreprintServer
from app.tools.article_search.pubmed import (
    PubMedArticle,
    PubMedAuthor,
    PubMedSearchResult,
    PubMedSearchStatus,
)
from app.tools.article_search.semantic_scholar import (
    SemanticScholarArticle,
    SemanticScholarAuthor,
    SemanticScholarFullTextResource,
)


NOW = datetime(2026, 8, 17, tzinfo=UTC)


def test_identifier_normalization_is_conservative() -> None:
    assert normalize_doi(" https://doi.org/10.1000/Example ") == "10.1000/example"
    assert normalize_doi("not-a-doi") is None
    assert normalize_pmid(" 12345678 ") == "12345678"
    assert normalize_pmid("PMID123") is None
    assert normalize_pmcid(" pmc1234567 ") == "PMC1234567"
    assert normalize_pmcid("1234567") == "PMC1234567"


def test_normalizes_pubmed_without_claiming_downloadable_full_text() -> None:
    article = PubMedArticle(
        source_record_id="12345678",
        article_id="pubmed:pmid:12345678",
        pmid="12345678",
        pmcid="PMC1234567",
        doi="10.1000/Example",
        title="  Semaglutide   and Weight Loss ",
        abstract="BACKGROUND: Study.\nRESULTS: Weight decreased.",
        authors=[PubMedAuthor(full_name="Ying Zhang", first_name="Ying", last_name="Zhang")],
        first_author="Ying Zhang",
        journal_title="Example Journal",
        publication_date="2025-06-02",
        publication_year=2025,
        publication_types=["Journal Article"],
        has_full_text=False,
        landing_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
        retrieved_at=NOW,
        search_run_id="run-pubmed",
    )

    record = SearchResultsNormalizer().normalize_article(article, source_rank=1)

    assert record.source == SearchSource.PUBMED
    assert record.doi == "10.1000/example"
    assert record.normalized_title == "semaglutide and weight loss"
    assert record.abstract_available is True
    assert record.peer_review_status == PeerReviewStatus.UNKNOWN
    assert record.can_download_full_text is False
    assert record.source_rank == 1


def test_normalizes_europe_pmc_resources_without_losing_oa_fields() -> None:
    resource = FullTextResource(
        resource_id="europe:pdf:1",
        format=FullTextFormat.PDF,
        original_style="pdf",
        url="https://europepmc.org/articles/PMC1234567?pdf=render",
        site="Europe_PMC",
        availability="Open access",
        availability_code="OA",
        is_open_access=True,
        is_downloadable=True,
        suggested_file_name="article.pdf",
    )
    article = EuropePMCArticle(
        source_record_id="12345678",
        article_id="europe_pmc:MED:12345678",
        pmid="12345678",
        pmcid="PMC1234567",
        doi="10.1000/example",
        title="Semaglutide and weight loss",
        authors=[EuropePMCAuthor(full_name="Ying Zhang", orcid="0000-0001")],
        first_author="Ying Zhang",
        abstract="Weight decreased.",
        abstract_available=True,
        is_open_access=True,
        has_full_text=True,
        can_download_full_text=True,
        license="CC BY",
        full_text_resources=[resource],
        preferred_full_text_format=FullTextFormat.PDF,
        preferred_download_url=resource.url,
        cited_by_count=12,
        retrieved_at=NOW,
        search_run_id="run-europe",
    )

    record = SearchResultsNormalizer().normalize_article(article)

    assert record.source == SearchSource.EUROPE_PMC
    assert record.is_open_access is True
    assert record.can_download_full_text is True
    assert record.citation_count == 12
    assert record.full_text_resources[0].format == UnifiedFullTextFormat.PDF
    assert record.full_text_resources[0].is_downloadable is True


def test_normalizes_semantic_scholar_pmc_and_preprint_boundaries() -> None:
    semantic = SemanticScholarArticle(
        source_record_id="s2-1",
        article_id="semantic_scholar:paper:s2-1",
        title="Semantic record",
        abstract="An abstract.",
        abstract_available=True,
        authors=[SemanticScholarAuthor(author_id="a1", name="Ying Zhang")],
        first_author="Ying Zhang",
        is_open_access=True,
        has_full_text=True,
        full_text_resources=[
            SemanticScholarFullTextResource(
                resource_id="s2:pdf:1",
                url="https://example.test/s2.pdf",
                license="CC BY",
                suggested_file_name="s2.pdf",
            )
        ],
        citation_count=5,
        retrieved_at=NOW,
        search_run_id="run-s2",
    )
    pmc = PMCArticle(
        source_record_id="PMC1234567",
        article_id="pmc:pmcid:PMC1234567",
        pmcid="PMC1234567",
        title="PMC record",
        authors=[PMCAuthor(full_name="Ying Zhang")],
        has_full_text=True,
        can_download_full_text=False,
        landing_url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
        retrieved_at=NOW,
        search_run_id="run-pmc",
    )
    preprint = PreprintArticle(
        source="medrxiv",
        source_record_id="10.1101/2025.01.01.123456",
        article_id="medrxiv:doi:10.1101/2025.01.01.123456",
        doi="10.1101/2025.01.01.123456",
        title="Preprint record",
        authors=["Ying Zhang"],
        first_author="Ying Zhang",
        version=2,
        publication_type="new results",
        license="cc_by",
        jatsxml_url="https://example.test/preprint.xml",
        preprint_server=PreprintServer.MEDRXIV,
        peer_review_status="preprint",
        has_full_text=True,
        landing_url="https://www.medrxiv.org/content/10.1101/2025.01.01.123456v2",
        retrieved_at=NOW,
        search_run_id="run-preprint",
    )

    normalizer = SearchResultsNormalizer()
    semantic_record = normalizer.normalize_article(semantic)
    pmc_record = normalizer.normalize_article(pmc)
    preprint_record = normalizer.normalize_article(preprint)

    assert semantic_record.can_download_full_text is True
    assert semantic_record.authors[0].source_author_id == "a1"
    assert pmc_record.has_full_text is True
    assert pmc_record.can_download_full_text is False
    assert preprint_record.source == SearchSource.MEDRXIV
    assert preprint_record.peer_review_status == PeerReviewStatus.PREPRINT
    assert preprint_record.preprint_version == 2
    assert preprint_record.full_text_resources[0].format == UnifiedFullTextFormat.XML


def test_batch_normalization_marks_invalid_record_as_partial_success() -> None:
    valid = PubMedArticle(
        source_record_id="1",
        article_id="pubmed:pmid:1",
        pmid="1",
        title="Valid title",
        landing_url="https://pubmed.ncbi.nlm.nih.gov/1/",
        retrieved_at=NOW,
        search_run_id="run-1",
    )
    invalid = PubMedArticle(
        source_record_id="2",
        article_id="pubmed:pmid:2",
        pmid="2",
        title=None,
        landing_url="https://pubmed.ncbi.nlm.nih.gov/2/",
        retrieved_at=NOW,
        search_run_id="run-1",
    )
    provider_result = PubMedSearchResult(
        search_run_id="run-1",
        status=PubMedSearchStatus.SUCCESS_WITH_RESULTS,
        hit_count=2,
        retrieved_count=2,
        page_count=1,
        records=[valid, invalid],
        latency_ms=10,
    )

    result = SearchResultsNormalizer().normalize_result(provider_result)

    assert result.status == UnifiedSearchStatus.PARTIAL_SUCCESS
    assert result.retrieved_count == 1
    assert result.invalid_record_count == 1
    assert result.records[0].source_rank == 1


def test_unknown_article_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="不支持的文章模型"):
        SearchResultsNormalizer().normalize_article(object())  # type: ignore[arg-type]
