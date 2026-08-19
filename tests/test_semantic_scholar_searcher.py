from __future__ import annotations

import httpx
import pytest

from app.tools.article_search.semantic_scholar import (
    SemanticScholarSearcher,
    SemanticScholarSearchRequest,
    SemanticScholarSearchStatus,
    SemanticScholarSortMode,
)


def _paper() -> dict[str, object]:
    return {
        "paperId": "s2-paper-1",
        "externalIds": {
            "DOI": "10.1000/example",
            "PubMed": "12345678",
            "PubMedCentral": "PMC1234567",
        },
        "url": "https://www.semanticscholar.org/paper/s2-paper-1",
        "title": "Semaglutide and weight reduction",
        "abstract": "Body weight decreased.",
        "venue": "Example Journal",
        "year": 2025,
        "publicationDate": "2025-06-02",
        "publicationTypes": ["JournalArticle"],
        "authors": [{"authorId": "a1", "name": "Ying Zhang"}],
        "citationCount": 12,
        "influentialCitationCount": 3,
        "isOpenAccess": True,
        "openAccessPdf": {
            "url": "https://example.test/article.pdf",
            "status": "GREEN",
            "license": "CC BY",
        },
        "fieldsOfStudy": ["Medicine"],
    }


@pytest.mark.anyio
async def test_relevance_search_maps_article_and_open_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/paper/search")
        assert request.url.params["limit"] == "1"
        assert request.url.params["offset"] == "0"
        assert request.headers["x-api-key"] == "secret"
        return httpx.Response(200, json={"total": 1, "data": [_paper()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SemanticScholarSearcher(client=client).search(
            SemanticScholarSearchRequest(
                query="semaglutide weight loss",
                max_results=1,
                api_key="secret",
            )
        )

    assert result.status == SemanticScholarSearchStatus.SUCCESS_WITH_RESULTS
    assert result.retrieved_count == 1
    article = result.records[0]
    assert article.doi == "10.1000/example"
    assert article.pmcid == "PMC1234567"
    assert article.first_author == "Ying Zhang"
    assert article.has_full_text is True
    assert article.full_text_resources[0].url == "https://example.test/article.pdf"


@pytest.mark.anyio
async def test_newest_search_uses_bulk_endpoint_and_sort() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/paper/search/bulk")
        assert request.url.params["sort"] == "publicationDate:desc"
        return httpx.Response(200, json={"total": 0, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SemanticScholarSearcher(client=client).search(
            SemanticScholarSearchRequest(
                query="weight loss",
                sort_mode=SemanticScholarSortMode.NEWEST,
            )
        )

    assert result.status == SemanticScholarSearchStatus.SUCCESS_EMPTY


@pytest.mark.anyio
async def test_search_reports_retryable_http_failure() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, json={"message": "busy"})
        )
    ) as client:
        result = await SemanticScholarSearcher(
            client=client,
            max_retries=0,
        ).search(SemanticScholarSearchRequest(query="weight loss"))

    assert result.status == SemanticScholarSearchStatus.FAILED
    assert result.error is not None
    assert result.error.http_status == 503
    assert result.error.retryable is True
