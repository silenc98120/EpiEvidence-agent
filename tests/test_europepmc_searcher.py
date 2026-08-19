from __future__ import annotations

import httpx
import pytest

from app.tools.article_search.europepmc import (
    EuropePMCSearcher,
    EuropePMCSearchRequest,
    EuropePMCSearchStatus,
    FullTextFormat,
)


def _core_record(record_id: str = "12345678") -> dict:
    return {
        "id": record_id,
        "source": "MED",
        "pmid": record_id,
        "pmcid": "PMC1234567",
        "doi": "10.1000/example",
        "title": "Semaglutide treatment and body weight reduction",
        "authorString": "Zhang Y, Smith A",
        "authorList": {
            "author": [
                {
                    "fullName": "Zhang Y",
                    "firstName": "Ying",
                    "lastName": "Zhang",
                    "initials": "Y",
                }
            ]
        },
        "journalInfo": {
            "journal": {
                "title": "Example Medical Journal",
                "ISOAbbreviation": "Example Med J",
                "ISSN": "1234-5678",
                "ESSN": "8765-4321",
            }
        },
        "pubYear": "2025",
        "firstPublicationDate": "2025-06-01",
        "pubTypeList": {"pubType": ["Journal Article", "Clinical Trial"]},
        "abstractText": (
            "<h4>BACKGROUND</h4><p>Weight management was evaluated.</p>"
            "<h4>RESULTS</h4><p>Body weight decreased.</p>"
        ),
        "isOpenAccess": "Y",
        "inEPMC": "Y",
        "inPMC": "Y",
        "hasPDF": "Y",
        "license": "CC BY",
        "fullTextUrlList": {
            "fullTextUrl": [
                {
                    "availability": "Open access",
                    "availabilityCode": "OA",
                    "documentStyle": "html",
                    "site": "Europe_PMC",
                    "url": "https://europepmc.org/articles/PMC1234567",
                },
                {
                    "availability": "Open access",
                    "availabilityCode": "OA",
                    "documentStyle": "pdf",
                    "site": "Europe_PMC",
                    "url": "https://europepmc.org/articles/PMC1234567?pdf=render",
                },
                {
                    "availability": "Subscription required",
                    "availabilityCode": "S",
                    "documentStyle": "doi",
                    "site": "DOI",
                    "url": "https://doi.org/10.1000/example",
                },
            ]
        },
        "citedByCount": 12,
    }


@pytest.mark.anyio
async def test_search_maps_core_record_and_prefers_downloadable_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = request.content.decode()
        assert "resultType=core" in body
        assert "format=json" in body
        assert "synonym=false" in body
        return httpx.Response(
            200,
            json={
                "version": "6.9",
                "hitCount": 1,
                "nextCursorMark": "cursor-1",
                "resultList": {"result": [_core_record()]},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await EuropePMCSearcher(client=client).search(
            EuropePMCSearchRequest(
                query='MESH:"Semaglutide"',
                max_results=1,
            )
        )

    assert result.status == EuropePMCSearchStatus.SUCCESS_WITH_RESULTS
    assert result.retrieved_count == 1
    article = result.records[0]
    assert article.article_id == "europe_pmc:MED:12345678"
    assert article.publication_types == ["Journal Article", "Clinical Trial"]
    assert article.study_design is None
    assert article.abstract == (
        "BACKGROUND\nWeight management was evaluated.\n"
        "RESULTS\nBody weight decreased."
    )
    assert article.has_full_text is True
    assert article.can_download_full_text is True
    assert article.preferred_full_text_format == FullTextFormat.PDF
    assert article.preferred_download_url == (
        "https://europepmc.org/articles/PMC1234567?pdf=render"
    )
    assert len(article.full_text_resources) == 2


@pytest.mark.anyio
async def test_search_follows_cursor_and_respects_max_results() -> None:
    requested_cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        cursor = "*" if "cursorMark=%2A" in body else "cursor-2"
        requested_cursors.append(cursor)
        if cursor == "*":
            return httpx.Response(
                200,
                json={
                    "hitCount": 3,
                    "nextCursorMark": "cursor-2",
                    "resultList": {"result": [_core_record("1"), _core_record("2")]},
                },
            )
        return httpx.Response(
            200,
            json={
                "hitCount": 3,
                "nextCursorMark": "cursor-3",
                "resultList": {"result": [_core_record("3")]},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await EuropePMCSearcher(client=client).search(
            EuropePMCSearchRequest(
                query='TITLE_ABS:"weight loss"',
                page_size=2,
                max_results=3,
            )
        )

    assert requested_cursors == ["*", "cursor-2"]
    assert result.page_count == 2
    assert result.hit_count == 3
    assert [record.source_record_id for record in result.records] == ["1", "2", "3"]


@pytest.mark.anyio
async def test_search_returns_partial_success_when_later_page_fails() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "hitCount": 2,
                    "nextCursorMark": "cursor-2",
                    "resultList": {"result": [_core_record("1")]},
                },
            )
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await EuropePMCSearcher(
            client=client,
            max_retries=0,
        ).search(
            EuropePMCSearchRequest(
                query='TITLE_ABS:"weight loss"',
                page_size=1,
                max_results=2,
            )
        )

    assert result.status == EuropePMCSearchStatus.PARTIAL_SUCCESS
    assert result.retrieved_count == 1
    assert result.error is not None
    assert result.error.http_status == 503
    assert result.error.retryable is True


@pytest.mark.anyio
async def test_search_distinguishes_a_valid_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hitCount": 0,
                "resultList": {"result": []},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        result = await EuropePMCSearcher(client=client).search(
            EuropePMCSearchRequest(query='TITLE:"no such article"')
        )

    assert result.status == EuropePMCSearchStatus.SUCCESS_EMPTY
    assert result.records == []
    assert result.error is None
