from __future__ import annotations

import httpx
import pytest

from app.tools.article_search.pmc import (
    PMCSearcher,
    PMCSearchRequest,
    PMCSearchStatus,
)


def _jats_xml() -> str:
    return """<?xml version="1.0"?>
<pmc-articleset>
  <article article-type="research-article">
    <front>
      <journal-meta><journal-title-group><journal-title>Example Journal</journal-title></journal-title-group></journal-meta>
      <article-meta>
        <article-id pub-id-type="pmcid">PMC1234567</article-id>
        <article-id pub-id-type="pmid">12345678</article-id>
        <article-id pub-id-type="doi">10.1000/example</article-id>
        <title-group><article-title>Semaglutide <italic>and</italic> weight loss</article-title></title-group>
        <contrib-group><contrib contrib-type="author"><name><surname>Zhang</surname><given-names>Ying</given-names></name></contrib></contrib-group>
        <pub-date pub-type="epub"><day>2</day><month>6</month><year>2025</year></pub-date>
        <abstract><p>Body weight decreased.</p></abstract>
        <permissions><license href="https://creativecommons.org/licenses/by/4.0/" /></permissions>
      </article-meta>
    </front>
  </article>
</pmc-articleset>"""


@pytest.mark.anyio
async def test_pmc_search_runs_esearch_and_efetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["db"] == "pmc"
            assert request.url.params["sort"] == "relevance"
            return httpx.Response(
                200,
                json={"esearchresult": {"count": "1", "idlist": ["1234567"]}},
            )
        assert request.url.path.endswith("efetch.fcgi")
        assert request.url.params["db"] == "pmc"
        assert request.url.params["id"] == "1234567"
        return httpx.Response(200, text=_jats_xml())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCSearcher(client=client).search(
            PMCSearchRequest(query='"Semaglutide"[Title/Abstract]', max_results=1)
        )

    assert result.status == PMCSearchStatus.SUCCESS_WITH_RESULTS
    article = result.records[0]
    assert article.pmcid == "PMC1234567"
    assert article.pmid == "12345678"
    assert article.doi == "10.1000/example"
    assert article.title == "Semaglutide and weight loss"
    assert article.first_author == "Ying Zhang"
    assert article.publication_date == "2025-06-02"
    assert article.abstract == "Body weight decreased."
    assert article.has_full_text is True
    assert article.can_download_full_text is False


@pytest.mark.anyio
async def test_pmc_search_distinguishes_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"esearchresult": {"count": "0", "idlist": []}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCSearcher(client=client).search(
            PMCSearchRequest(query="no such article")
        )

    assert result.status == PMCSearchStatus.SUCCESS_EMPTY
    assert result.records == []
