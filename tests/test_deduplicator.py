from __future__ import annotations

from datetime import UTC, datetime

from app.core.deduplicator import (
    DeduplicationKeyKind,
    Deduplicator,
    build_deduplication_keys,
)
from app.core.search_results_normalizer import (
    EvidenceRecord,
    FullTextResourceCandidate,
    UnifiedFullTextFormat,
)
from app.core.search_strategy_generator import SearchSource


NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _record(
    *,
    source: SearchSource = SearchSource.PUBMED,
    title: str = "Semaglutide and weight loss",
    doi: str | None = None,
    pmid: str | None = None,
    pmcid: str | None = None,
    first_author: str = "Ying Zhang",
    publication_year: int | None = 2025,
    abstract: str | None = None,
    can_download_full_text: bool = False,
    resources: list[FullTextResourceCandidate] | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        source=source,
        source_record_id=f"{source.value}:{doi or pmid or pmcid or title}",
        source_article_id=f"{source.value}:article:{doi or pmid or pmcid or title}",
        search_run_id=f"run:{source.value}",
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        title=title,
        normalized_title=" ".join(title.casefold().split()),
        abstract=abstract,
        abstract_available=abstract is not None,
        first_author=first_author,
        publication_year=publication_year,
        can_download_full_text=can_download_full_text,
        full_text_resources=resources or [],
        retrieved_at=NOW,
    )


def test_strong_identifier_dedup_keeps_all_source_provenance() -> None:
    pubmed = _record(
        source=SearchSource.PUBMED,
        doi="10.1000/Example",
        pmid="12345678",
        abstract="Short abstract.",
    )
    europe = _record(
        source=SearchSource.EUROPE_PMC,
        doi="10.1000/example",
        pmid="12345678",
        abstract="A longer abstract from Europe PMC.",
    )

    result = Deduplicator().deduplicate([pubmed, europe])

    assert result.input_count == 2
    assert result.unique_count == 1
    assert result.duplicate_count == 1
    assert result.clusters[0].duplicate_count == 1
    assert {member.source for member in result.clusters[0].members} == {
        SearchSource.PUBMED,
        SearchSource.EUROPE_PMC,
    }
    assert {key.kind for key in result.clusters[0].matched_by} == {
        DeduplicationKeyKind.DOI,
        DeduplicationKeyKind.PMID,
    }
    assert result.records[0].abstract == "A longer abstract from Europe PMC."


def test_title_fallback_requires_year_and_first_author() -> None:
    first = _record(doi=None, pmid=None, pmcid=None)
    same_work = _record(
        source=SearchSource.SEMANTIC_SCHOLAR,
        doi=None,
        pmid=None,
        pmcid=None,
        title="  Semaglutide and   weight loss ",
    )
    different_author = _record(
        source=SearchSource.PMC,
        doi=None,
        pmid=None,
        pmcid=None,
        first_author="Li Wei",
    )

    result = Deduplicator().deduplicate([first, same_work, different_author])

    assert result.unique_count == 2
    assert result.duplicate_count == 1
    assert result.clusters[0].matched_by[0].kind == DeduplicationKeyKind.TITLE_YEAR_AUTHOR


def test_different_strong_identifiers_do_not_merge_by_title_alone() -> None:
    first = _record(doi="10.1000/first", pmid="100")
    second = _record(doi="10.1000/second", pmid="200")

    result = Deduplicator().deduplicate([first, second])

    assert result.unique_count == 2
    assert result.duplicate_count == 0


def test_transitive_identifier_match_forms_one_cluster_and_records_conflict() -> None:
    first = _record(doi="10.1000/shared", pmid="100")
    second = _record(
        source=SearchSource.EUROPE_PMC,
        doi="10.1000/shared",
        pmid="200",
    )
    third = _record(source=SearchSource.PMC, pmid="200", doi=None)

    result = Deduplicator().deduplicate([first, second, third])

    assert result.unique_count == 1
    assert result.duplicate_count == 2
    assert result.conflict_count == 1
    assert len(result.clusters[0].members) == 3


def test_full_text_resources_are_merged_without_duplicates() -> None:
    resource = FullTextResourceCandidate(
        source=SearchSource.EUROPE_PMC,
        resource_id="resource-1",
        format=UnifiedFullTextFormat.PDF,
        url="https://example.test/article.pdf",
        is_downloadable=True,
    )
    first = _record(
        doi="10.1000/shared",
        resources=[resource],
        can_download_full_text=True,
    )
    second = _record(
        source=SearchSource.EUROPE_PMC,
        doi="10.1000/shared",
        resources=[resource],
        can_download_full_text=True,
    )

    result = Deduplicator().deduplicate([first, second])

    assert len(result.records[0].full_text_resources) == 1
    assert result.records[0].can_download_full_text is True


def test_build_keys_does_not_create_title_key_without_year_or_author() -> None:
    record = _record(
        doi=None,
        pmid=None,
        pmcid=None,
        first_author="",
        publication_year=None,
    )

    assert build_deduplication_keys(record) == []
