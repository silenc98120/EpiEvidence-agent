# Europe PMC Compiler And Search Tool Research

> Status: search request and response-resource contract approved; Europe PMC Searcher implemented. Full-text downloading remains a separate later unit.
> Researched: 2026-08-12
> Scope: Europe PMC advanced-query compilation, REST request contract, pagination, response fields, and the EpiEvidence MVP boundary.

## 1. Sources And Verification Method

Primary sources:

- Europe PMC Articles RESTful API: <https://europepmc.org/RestfulWebService>
- Official Swagger 2.0 document: <https://www.ebi.ac.uk/europepmc/webservices/api/swagger.json>
- Official indexed-fields endpoint: <https://www.ebi.ac.uk/europepmc/webservices/rest/fields?format=json>
- Europe PMC Web Service Reference Guide 6.9: <https://europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf>
- Europe PMC Search Syntax Guide: <https://europepmc.org/searchSyntaxGuide>

The API behavior below was also checked with read-only requests against the production REST service. Tests covered `MESH`, `KW`, `TITLE_ABS`, `TITLE`, `ABSTRACT`, Boolean grouping, `NOT`, date ranges, trailing wildcards, `idlist`/`lite`/`core`, sorting, POST form requests, OA metadata, and two-page cursor pagination.

## 2. Main Findings

### 2.1 Europe PMC does not build our SearchPlan

Europe PMC accepts a completed query string. EpiEvidence must retain responsibility for:

```text
IntentRecognitionResult
+ MeshNormalizationResult
-> provider-neutral SearchPlan
-> Europe PMC query compiler
-> Europe PMC search request
```

The LLM may provide English free-text candidates upstream, but it should not emit Europe PMC field syntax.

### 2.2 Query fields needed by the MVP

| Purpose | Europe PMC expression | Decision status |
|---|---|---|
| Confirmed MeSH preferred label | `MESH:"Semaglutide"` | Production API verified; retain for controlled headings |
| Title or abstract free text | `TITLE_ABS:"weight loss"` | Official indexed field and API verified |
| MeSH plus publisher keywords | `KW:"Semaglutide"` | Official field, but broader than `MESH`; do not substitute for a confirmed heading |
| Publication date range | `FIRST_PDATE:[2018-01-01 TO 2020-12-31]` | Official reference and API verified |
| Require an abstract | `HAS_ABSTRACT:Y` | Official field; product decision pending |
| Open-access only | `OPEN_ACCESS:Y` | Supported, but not recommended as a default retrieval filter |
| Full text in PMC | `IN_PMC:Y` | Supported; useful for filtering or display, not quality ranking |
| PDF available | `HAS_PDF:Y` | Supported; useful for display or later reading flows |
| Publication type | `PUB_TYPE:"Review"` | Supported |

`MESH` is accepted by the production API and returns a narrower result set than `KW`. It is not present in the current `/fields` enumeration, whereas `KW` is documented as including MeSH and publisher-supplied keywords. Therefore the first compiler can use `MESH` for the intended controlled-heading semantics, but a live contract test should protect this behavior.

### 2.3 Boolean and phrase composition

The MVP compiler should generate:

```text
same concept:      term OR term OR term
different concept: group AND group AND group
```

Each group must be parenthesized. Multi-word phrases must be double quoted. Verified examples include:

```text
(MESH:"Semaglutide" OR TITLE_ABS:"Ozempic" OR TITLE_ABS:"Wegovy")
AND
(MESH:"Obesity" OR TITLE_ABS:"people with obesity")
AND
(TITLE_ABS:"weight loss" OR TITLE_ABS:"body weight reduction")
```

The production API also accepts `NOT`, date ranges using `[start TO end]`, and trailing wildcards such as `TITLE_ABS:semaglut*`. Wildcards and proximity/fuzzy operators are not required by the first SearchPlan and should not be generated automatically.

Compiler escaping remains deterministic:

- trim and collapse whitespace;
- escape backslashes and double quotes inside phrases;
- deduplicate terms case-insensitively while preserving order;
- never parse an already-compiled query back into concepts;
- never log the raw medical query.

### 2.4 Synonym expansion

The REST API supports `synonym=true`, which expands queries with MeSH terminology and UniProt synonyms. It is disabled by default.

EpiEvidence already explicitly stores MeSH Preferred Labels, Entry Terms, and LLM free-text candidates. The proposed first-version request therefore uses:

```text
synonym=false
```

This avoids an opaque second expansion that is not represented in `SearchPlan` and would weaken reproducibility.

## 3. Public REST Request Contract

### 3.1 Endpoint and method

Europe PMC supports both GET `/search` and POST `/searchPOST`. The POST contract is better for EpiEvidence because OR-expanded queries can become long.

```http
POST https://www.ebi.ac.uk/europepmc/webservices/rest/searchPOST
Content-Type: application/x-www-form-urlencoded
Accept: application/json
```

The request body is URL-encoded form data, not JSON:

```text
query=<compiled Europe PMC query>
resultType=core
format=json
pageSize=100
cursorMark=*
synonym=false
```

No API key is required. The optional `email` parameter allows EBI to contact API users about service news; the official pages inspected did not publish a fixed requests-per-second limit.

### 3.2 Supported request parameters

| Parameter | Meaning | MVP proposal |
|---|---|---|
| `query` | Completed Europe PMC query string | Required |
| `resultType` | `idlist`, `lite`, or `core` | Always `core` |
| `format` | `xml`, `json`, or `dc` | Always `json` |
| `pageSize` | Results per page, 1-1000, default 25 | Configurable; proposed 100 |
| `cursorMark` | First page `*`; then use returned cursor | Required for bounded pagination |
| `sort` | Default relevance, or `<field> asc/desc` | Omit to preserve relevance |
| `synonym` | Europe PMC automatic expansion | `false` |
| `email` | Optional contact email | Optional environment configuration |
| `callback` | JSONP callback | Not used |

### 3.3 Sorting

When `sort` is omitted, Europe PMC uses relevance. Verified explicit examples include:

```text
FIRST_PDATE_D desc
FIRST_PDATE_D asc
CITED desc
```

The EpiEvidence product policy is relevance first, then publication time within relevance tiers. Therefore the initial retrieval should preserve Europe PMC relevance order. Recency should be applied later by the local ranking stage, using the returned publication date.

### 3.4 Pagination

The first page sends `cursorMark=*` or omits it. The response returns `nextCursorMark`. Each following request sends that value unchanged.

Stop pagination when any of these conditions is true:

- the configured maximum result count has been collected;
- `resultList.result` is empty;
- `nextCursorMark` is missing or unchanged;
- a configured page limit is reached;
- the request fails after bounded retries.

The production POST endpoint was verified across two cursor pages.

## 4. Response Formats

### 4.1 Top-level JSON

All three response types share this general shape:

```json
{
  "version": "6.9",
  "hitCount": 5287,
  "nextCursorMark": "...",
  "nextPageUrl": "...",
  "request": {
    "queryString": "TITLE_ABS:\"Semaglutide\"",
    "resultType": "core",
    "cursorMark": "*",
    "pageSize": 1,
    "sort": "",
    "synonym": false
  },
  "resultList": {
    "result": []
  }
}
```

`nextPageUrl` can be absent in some response variants. The client should construct the next POST body from `nextCursorMark` rather than following `nextPageUrl`.

### 4.2 `idlist`

Returns identifiers only. A verified record contained:

```text
id, source, pmid
```

It is insufficient for the EpiEvidence MVP.

### 4.3 `lite`

Returns compact bibliographic metadata such as:

```text
id, source, pmid, doi, title, authorString, journalTitle,
pubYear, journalIssn, pubType, isOpenAccess, inEPMC, inPMC,
hasPDF, citedByCount, firstPublicationDate
```

It does not provide the abstract needed for relevance screening.

### 4.4 `core`

Returns the fields needed by the first version:

```text
id, source, pmid, pmcid, doi, title,
authorString, authorList,
journalInfo, pubYear, firstPublicationDate,
abstractText, language, publicationStatus, pubModel,
pubTypeList, keywordList, meshHeadingList,
fullTextUrlList, isOpenAccess, inEPMC, inPMC, hasPDF,
license, citedByCount,
firstIndexDate, electronicPublicationDate
```

Fields are record-dependent and must be optional. For example, a `core` result can lack `pmcid`, `license`, `meshHeadingList`, `abstractText`, or a PDF URL.

## 5. Fields EpiEvidence Should Keep

The connector should validate a source-specific raw page, then normalize each record into the later shared evidence model. The proposed retained fields are:

### Identity and provenance

```text
source = "europe_pmc"
source_record_id
source_database_code       # MED, PMC, PPR, etc.
pmid
pmcid
doi
retrieved_at
compiled_query_id or search_run_id
```

### Citation metadata

```text
title
authors[]                  # structured list, not semicolon text
journal_title
issn
publication_year
first_publication_date
language
publication_status
publication_types[]        # Europe PMC pubTypeList; for example Journal Article or Review
study_design               # later screening result; not guessed by the connector
keywords[]
abstract_raw               # source HTML, retained outside LangGraph messages
abstract                   # sanitized plain text
abstract_available
```

`publication_types[]` is the source-reported publication type and is retained as the
first-version "research type" field. It must remain separate from `study_design`.
For example, Europe PMC may report `Journal Article`, while deciding that the article
is a randomized controlled trial or cohort study can require reading its abstract or
methods. The connector must not invent `study_design` when the source does not provide
enough information.

### Availability and ranking signals

```text
is_open_access             # true / false / unknown
in_europe_pmc
in_pmc
has_pdf                    # source flag; true / false / unknown
has_full_text              # at least one usable full-text resource exists
license
landing_url
full_text_resources[]      # all available HTML/PDF/other resources
cited_by_count
```

OA status should be derived from the explicit flags and `fullTextUrlList`; it must not be inferred from the presence of a DOI link. A DOI URL can still require a subscription.

Each full-text resource keeps its own format rather than assigning one format to the
whole article:

```text
FullTextResource
  url
  style                     # html | pdf | xml | text | other
  site
  availability
  availability_code
  is_open_access            # true / false / unknown for this resource
  is_downloadable           # true / false / unknown
```

An article can therefore expose both HTML and PDF resources. `full_text_styles` can be
derived from `full_text_resources[].style` for display, but should not be stored as a
single scalar field. Preferred-link selection remains:

```text
Europe PMC open-access PDF
-> Europe PMC open-access HTML
-> another explicitly open-access PDF/HTML
-> DOI landing page
```

Missing-value policy:

- Text fields such as `abstract`, `doi`, and `license` use `null` when absent.
- Collections such as `publication_types` and `full_text_resources` use `[]` when empty.
- Boolean availability fields use `true`, `false`, or `null` when the source is unknown.
- Empty strings are not used in the runtime data model; the API/UI may render `null` as
  a blank cell.

This preserves the distinction between `false` (Europe PMC explicitly says the resource
is unavailable) and `null` (Europe PMC did not provide enough information).

The following large or source-specific structures should not be copied wholesale into LangGraph `messages`: full author affiliations, all grants, all subsets, all database cross-references, and the full raw JSON. If retained for audit, raw JSON belongs in a persistence/debug artifact outside the prompt context.

## 6. Proposed Runtime Models

The following are design contracts, not implemented code:

```text
EuropePMCSearchRequest
  compiled_query
  max_results
  page_size
  cursor_mark
  result_type = core
  synonym = false
  sort = relevance (represented by omitting sort)

EuropePMCSearchPage
  api_version
  hit_count
  next_cursor_mark
  request_echo
  records[]

EuropePMCSearchResult
  source
  status
  hit_count
  retrieved_count
  records[]
  page_count
  latency_ms
  error
```

The tool should distinguish:

```text
success_with_results
success_empty
partial_success
failed
```

Returning `[]` for every exception would erase the difference between a valid zero-result search and an upstream failure.

## 7. Error, Retry, And Monitoring Contract

### Validation

- Reject an empty compiled query.
- Enforce `1 <= page_size <= 1000` locally.
- Enforce positive `max_results` and an application-level upper bound.
- Validate the top-level JSON shape before parsing records.
- Treat missing optional record fields as `None`/empty collections, not fabricated values.
- Preserve HTML in `abstractText` only until a dedicated sanitizer converts it to plain text.

### Retries

- Retry only transient network failures, timeouts, HTTP 429, and HTTP 5xx.
- Use bounded exponential backoff with jitter.
- Do not retry invalid query syntax or other deterministic 4xx errors.
- Preserve already retrieved pages and report `partial_success` if a later page fails.

### Monitoring without sensitive query logging

Record:

```text
source
search_run_id
query_length
page_size
page_count
hit_count
retrieved_count
OA count
abstract-present count
latency_ms
retry_count
HTTP status / error type
```

Do not log the raw query or abstracts.

## 8. Decisions Requiring User Approval

### Decision 1: Abstract filter

Recommended: append `HAS_ABSTRACT:Y` to the first-version query because the next stage explicitly screens abstracts. This reduces unusable records but excludes potentially relevant citations without an abstract.

Alternative: retrieve records without this filter and mark `abstract_available=false`; those records cannot enter automatic screening.

### Decision 2: Retrieval limit

Recommended first default: `page_size=100`, `max_results=200`. This allows two cursor pages and keeps the relevance-screening cost bounded.

Alternative: smaller (`50/100`) for speed or larger (`200/500`) for recall.

### Decision 3: Date constraints

Recommended: add `start_date`/`end_date` to `SearchPlan` only when the user explicitly asks for a date range. Compile it as `FIRST_PDATE:[start TO end]`. Do not impose a default recent-year filter.

### Decision 4: OA policy

Recommended: do not append `OPEN_ACCESS:Y` or `IN_PMC:Y` during search. Retrieve all matching abstracts and record OA/full-text availability for display. This avoids availability bias.

### Decision 5: `MESH` contract risk

Recommended: retain `MESH:"preferred label"` because production behavior is verified and it gives the intended narrower controlled-heading search. Add an optional live integration test so upstream removal is detected. Do not replace it with broader `KW`.

### Decision 6: API email

Recommended: support an optional `EUROPE_PMC_EMAIL` environment variable and send it only when configured. It is not authentication and should not block use.

## 9. Proposed First Implementation Boundary

After approval, implement only:

1. finalize the Europe PMC compiler rules and optional filters;
2. define request, raw page, record, and result Pydantic schemas;
3. implement asynchronous POST form requests with cursor pagination;
4. map `core` records into typed Europe PMC records;
5. add bounded retries, Loguru metrics, and mocked unit tests;
6. add one optional live contract test excluded from the default test suite.

Do not yet implement LangGraph fan-out/fan-in, cross-database deduplication, abstract relevance scoring, or PubMed retrieval in this unit.

## 10. Implemented Searcher Boundary

Implemented in `app/tools/article_search/europepmc.py`:

- typed request, raw-page, normalized article, full-text resource, error, and result models;
- asynchronous form POST with `core` JSON and cursor pagination;
- bounded transient retries and explicit success/empty/partial/failed statuses;
- bibliographic, abstract, publication-type, journal, access, and resource mapping;
- PDF-first preferred-resource selection while retaining all readable resource formats;
- exclusion of DOI landing pages from directly downloadable resources;
- Loguru metrics without raw medical query or abstract content.

The searcher does not fetch the returned URL. Therefore `can_download_full_text` means
Europe PMC metadata exposes an explicitly open-access full-text resource URL. The later
download unit must still verify redirects, HTTP status, content type, file signature,
size, and local persistence before reporting a completed download.

The approved human-in-the-loop flow is:

```text
search and resource discovery
-> abstract screening and preliminary recommendation
-> show available original-file formats and names
-> user selects papers
-> later full-text downloader and parser
-> later full-text appraisal
```
