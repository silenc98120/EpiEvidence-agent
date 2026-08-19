from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.tools.article_search.biorxiv import BioRxivSearcher
from app.tools.article_search.medrxiv import MedRxivSearcher
from app.tools.article_search.preprint import (
    PreprintLookupRequest,
    PreprintSearchStatus,
    PreprintServer,
)


def _preprint(doi: str, server: str) -> dict[str, object]:
    return {
        "doi": doi,
        "title": "A preprint about weight loss",
        "authors": "Ying Zhang; Wei Li",
        "author_corresponding": "Ying Zhang",
        "author_corresponding_institution": "Example University",
        "date": "2025-06-02",
        "version": "2",
        "type": "new results",
        "license": "cc_by",
        "category": "endocrinology",
        "jatsxml": "https://example.test/article.xml",
        "abstract": "Body weight decreased.",
        "published": "10.1000/published",
        "server": server,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("searcher_type", "server"),
    [
        (BioRxivSearcher, PreprintServer.BIORXIV),
        (MedRxivSearcher, PreprintServer.MEDRXIV),
    ],
)
async def test_preprint_searcher_validates_doi_and_maps_latest_version(
    searcher_type,
    server: PreprintServer,
) -> None:
    doi = "10.1101/2025.01.01.123456"

    def handler(request: httpx.Request) -> httpx.Response:
        assert f"/details/{server.value}/" in request.url.path
        first = _preprint(doi, server.value)
        first["version"] = "1"
        return httpx.Response(
            200,
            json={"messages": [{"count": 2}], "collection": [first, _preprint(doi, server.value)]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await searcher_type(client=client).search(
            PreprintLookupRequest(dois=[doi])
        )

    assert result.status == PreprintSearchStatus.SUCCESS_WITH_RESULTS
    article = result.records[0]
    assert article.preprint_server == server
    assert article.version == 2
    assert article.first_author == "Ying Zhang"
    assert article.peer_review_status == "preprint"
    assert article.has_full_text is True


@pytest.mark.anyio
async def test_preprint_searcher_returns_partial_success() -> None:
    good_doi = "10.1101/2025.01.01.111111"
    bad_doi = "10.1101/2025.01.01.222222"

    def handler(request: httpx.Request) -> httpx.Response:
        if bad_doi.split("/")[1] in request.url.path:
            return httpx.Response(503, json={"message": "busy"})
        return httpx.Response(
            200,
            json={"collection": [_preprint(good_doi, "medrxiv")]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MedRxivSearcher(client=client, max_retries=0).search(
            PreprintLookupRequest(dois=[good_doi, bad_doi], concurrency=2)
        )

    assert result.status == PreprintSearchStatus.PARTIAL_SUCCESS
    assert result.retrieved_count == 1
    assert result.failed_lookup_count == 1


def test_preprint_request_rejects_more_than_100_dois() -> None:
    with pytest.raises(ValidationError):
        PreprintLookupRequest(
            dois=[f"10.1101/2025.01.01.{index:06d}" for index in range(101)]
        )
