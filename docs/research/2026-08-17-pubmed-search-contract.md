# PubMed Search Contract

## Scope

`app/tools/article_search/pubmed.py` implements the first PubMed connector for the MVP.
It retrieves title/abstract metadata only. It does not infer Open Access from a PMCID and
does not download full text.

## Request flow

For each bounded page, the connector makes two NCBI E-utilities requests:

1. `ESearch` with `db=pubmed`, the compiled query in `term`, `retmode=json`, `retstart`,
   `retmax`, and `sort=relevance`.
2. `EFetch` with `db=pubmed`, a comma-separated PMID list in `id`, and `retmode=xml`.

`email`, `tool`, and optional `api_key` are forwarded to NCBI. `max_results` and
`max_pages` bound the work. Network failures, HTTP 429/5xx responses, and timeouts use
finite exponential-backoff retries.

## Normalized output

`PubMedSearchResult` records:

- `hit_count`, `retrieved_count`, `page_count`, `retry_count`, and `latency_ms`;
- a status of `success_with_results`, `success_empty`, `partial_success`, or `failed`;
- `PubMedArticle` records containing PMID, optional PMCID/DOI, title, authors, journal,
  ISSN values, publication date/year, publication types, language, keywords, and a
  section-preserving plain-text abstract;
- a PubMed landing URL for each record.

An EFetch record without a PMID is rejected rather than inserted as an unstable article.
The provider-specific result is converted to a shared evidence contract before it enters
the Graph fan-in node; raw provider payloads belong in `source_records.raw_payload`.
