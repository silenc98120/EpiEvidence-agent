from __future__ import annotations

import hashlib

import httpx
import pytest

from app.tools.article_search.pmc import (
    PMCDownloadRequest,
    PMCDownloadStatus,
    PMCOADownloader,
    PMCOAResolver,
    PMCResourceFormat,
    PMCResolutionStatus,
)


def _oa_xml(*, include_pdf: bool = True) -> str:
    pdf = (
        '<link format="pdf" updated="2026-01-01" '
        'href="https://ftp.ncbi.nlm.nih.gov/pub/pmc/pdf/PMC1234567.pdf" />'
        if include_pdf
        else ""
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<OA><records><record id="PMC1234567" license="CC BY" retracted="no">
  {pdf}
  <link format="tgz" href="ftp://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/a/b/PMC1234567.tar.gz" />
</record></records></OA>"""


@pytest.mark.anyio
async def test_oa_resolver_parses_license_and_prefers_pdf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "PMC1234567"
        return httpx.Response(200, text=_oa_xml())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCOAResolver(client=client).resolve(" pmc1234567 ")

    assert result.status == PMCResolutionStatus.AVAILABLE
    assert result.license == "CC BY"
    assert result.retracted is False
    assert result.preferred_resource is not None
    assert result.preferred_resource.format == PMCResourceFormat.PDF
    assert result.preferred_resource.url.startswith("https://ftp.ncbi.nlm.nih.gov")
    assert {item.format for item in result.resources} == {
        PMCResourceFormat.PDF,
        PMCResourceFormat.TGZ,
    }


@pytest.mark.anyio
async def test_oa_resolver_returns_not_available_without_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<OA><records /></OA>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCOAResolver(client=client).resolve("PMC1234567")

    assert result.status == PMCResolutionStatus.NOT_AVAILABLE
    assert result.preferred_resource is None
    assert result.resources == []


@pytest.mark.anyio
async def test_downloader_writes_file_and_hashes_content(tmp_path) -> None:
    body = b"%PDF-1.7 example"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("oa.fcgi"):
            return httpx.Response(200, text=_oa_xml())
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/pdf"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCOADownloader(
            client=client,
            timeout_seconds=5,
        ).download(
            PMCDownloadRequest(
                pmcid="PMC1234567",
                destination_dir=tmp_path,
            )
        )

    assert result.status == PMCDownloadStatus.SUCCESS
    assert result.format == PMCResourceFormat.PDF
    assert result.bytes_written == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.local_path is not None
    assert result.local_path.endswith("PMC1234567.pdf")
    assert (tmp_path / "PMC1234567.pdf").read_bytes() == body


@pytest.mark.anyio
async def test_downloader_reports_unavailable_resource_without_writing(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<OA><records /></OA>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PMCOADownloader(
            client=client,
            max_retries=0,
        ).download(
            PMCDownloadRequest(
                pmcid="PMC1234567",
                destination_dir=tmp_path,
            )
        )

    assert result.status == PMCDownloadStatus.NOT_AVAILABLE
    assert result.local_path is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_downloader_does_not_overwrite_existing_file(tmp_path) -> None:
    target = tmp_path / "article.pdf"
    target.write_bytes(b"original")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"replacement")
        )
    ) as client:
        result = await PMCOADownloader(client=client).download(
            PMCDownloadRequest(
                resource_url="https://example.test/article.pdf",
                destination_dir=tmp_path,
                file_name=target.name,
            )
        )

    assert result.status == PMCDownloadStatus.FAILED
    assert result.error is not None
    assert result.error.error_type == "file_exists"
    assert target.read_bytes() == b"original"
