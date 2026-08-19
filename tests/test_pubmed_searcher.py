from __future__ import annotations

import httpx
import pytest

from app.tools.article_search.pubmed import (
    PubMedSearcher,
    PubMedSearchRequest,
    PubMedSearchStatus,
)


def _efetch_xml(pmid: str = "12345678") -> str:
    return f"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <ArticleTitle>Semaglutide <i>and</i> body weight reduction</ArticleTitle>
        <Journal>
          <Title>Example Medical Journal</Title>
          <ISOAbbreviation>Example Med J</ISOAbbreviation>
          <ISSN IssnType="Print">1234-5678</ISSN>
          <ISSN IssnType="Electronic">8765-4321</ISSN>
          <JournalIssue>
            <PubDate><Year>2025</Year><Month>Jun</Month><Day>01</Day></PubDate>
          </JournalIssue>
        </Journal>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
          <PublicationType>Clinical Trial</PublicationType>
        </PublicationTypeList>
        <AuthorList>
          <Author>
            <LastName>Zhang</LastName><ForeName>Ying</ForeName><Initials>Y</Initials>
          </Author>
        </AuthorList>
        <Language>eng</Language>
        <Abstract>
          <AbstractText Label="BACKGROUND">Weight management was evaluated.</AbstractText>
          <AbstractText Label="RESULTS">Body weight decreased.</AbstractText>
        </Abstract>
        <ArticleDate><Year>2025</Year><Month>06</Month><Day>02</Day></ArticleDate>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1000/example</ArticleId>
        <ArticleId IdType="pmc">PMC1234567</ArticleId>
      </ArticleIdList>
      <PublicationStatus>ppublish</PublicationStatus>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


@pytest.mark.anyio
async def test_search_runs_esearch_and_efetch_and_normalizes_article() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["db"] == "pubmed"
            assert request.url.params["retmode"] == "json"
            assert request.url.params["sort"] == "relevance"
            return httpx.Response(
                200,
                json={
                    "esearchresult": {
                        "count": "1",
                        "idlist": ["12345678"],
                    }
                },
            )
        assert request.url.path.endswith("efetch.fcgi")
        assert request.url.params["db"] == "pubmed"
        assert request.url.params["retmode"] == "xml"
        assert request.url.params["id"] == "12345678"
        return httpx.Response(200, text=_efetch_xml())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PubMedSearcher(client=client).search(
            PubMedSearchRequest(query='"Semaglutide"[Title/Abstract]', max_results=1)
        )

    assert result.status == PubMedSearchStatus.SUCCESS_WITH_RESULTS
    assert result.hit_count == 1
    assert result.retrieved_count == 1
    article = result.records[0]
    assert article.article_id == "pubmed:pmid:12345678"
    assert article.doi == "10.1000/example"
    assert article.pmcid == "PMC1234567"
    assert article.title == "Semaglutide and body weight reduction"
    assert article.first_author == "Ying Zhang"
    assert article.issn == "1234-5678"
    assert article.electronic_issn == "8765-4321"
    assert article.publication_date == "2025-06-02"
    assert article.publication_types == ["Journal Article", "Clinical Trial"]
    assert article.abstract == (
        "BACKGROUND: Weight management was evaluated.\n"
        "RESULTS: Body weight decreased."
    )
    assert article.has_full_text is False


@pytest.mark.anyio
async def test_search_follows_esearch_offsets_and_respects_max_results() -> None:
    requested_offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            offset = request.url.params["retstart"]
            requested_offsets.append(offset)
            pmid = "1" if offset == "0" else "2"
            return httpx.Response(
                200,
                json={
                    "esearchresult": {
                        "count": "2",
                        "idlist": [pmid],
                    }
                },
            )
        pmid = request.url.params["id"]
        return httpx.Response(200, text=_efetch_xml(pmid))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PubMedSearcher(client=client).search(
            PubMedSearchRequest(query="weight loss", page_size=1, max_results=2)
        )

    assert requested_offsets == ["0", "1"]
    assert result.page_count == 2
    assert [article.pmid for article in result.records] == ["1", "2"]


@pytest.mark.anyio
async def test_search_distinguishes_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"esearchresult": {"count": "0", "idlist": []}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PubMedSearcher(client=client).search(
            PubMedSearchRequest(query="no such article")
        )

    assert result.status == PubMedSearchStatus.SUCCESS_EMPTY
    assert result.hit_count == 0
    assert result.records == []
    assert result.error is None


@pytest.mark.anyio
async def test_search_returns_partial_success_when_efetch_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(
                200,
                json={
                    "esearchresult": {
                        "count": "1",
                        "idlist": ["12345678"],
                    }
                },
            )
        return httpx.Response(503, text="temporarily unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PubMedSearcher(
            client=client,
            max_retries=0,
        ).search(PubMedSearchRequest(query="weight loss"))

    assert result.status == PubMedSearchStatus.FAILED
    assert result.retrieved_count == 0
    assert result.error is not None
    assert result.error.http_status == 503
    assert result.error.retryable is True

