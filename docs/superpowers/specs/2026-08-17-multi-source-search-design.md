# Multi-Source Literature Search Design

## Scope

This design extends EpiEvidence from PubMed and Europe PMC retrieval to a controlled
multi-source search workflow. The first implementation adds Semantic Scholar, PMC,
bioRxiv, and medRxiv while preserving provenance and producing one provider-neutral
record contract.

The MVP continues to recommend literature from titles and abstracts. PMC full-text
links are discovered, but full-text download, parsing, and methodological appraisal are
outside this scope.

## Fixed Product Decisions

- PubMed and Europe PMC are the default primary sources.
- Semantic Scholar is a supplementary source used when primary evidence is insufficient.
- PMC keyword search is supplementary and is enabled when the user requests OA/full
  text or when primary evidence is insufficient.
- PMC OA link resolution is a separate operation performed after deduplication.
- bioRxiv and medRxiv are searched only when the user permits preprints.
- Every source returns at most 100 records per search run.
- The normal default is relevance ordering. Explicit requests for recent evidence use
  publication-date descending order.
- An explicit date range is a hard filter. No date filter is invented when the user has
  not requested one.
- The LLM makes the normal sufficiency decision. Numeric thresholds are monitoring
  signals and a failure fallback only.

## Preprint Policy

The source decision happens before SearchPlan construction.

```text
Explicit request for latest, emerging, or preprint evidence
  -> include_preprints

Explicit request for peer-reviewed or formally published evidence
  -> peer_reviewed_only

No expressed preference
  -> pause with a clarification question
```

The clarification is asked once per research task. A supplementary search inherits the
resolved policy unless the user explicitly changes it.

The first version distinguishes only two publication-scope policies:

- `peer_reviewed_only`
- `include_preprints`

Specific publication types such as Review, Clinical Trial, and Randomized Controlled
Trial remain source metadata and future SearchPlan filters. They are not mixed with the
preprint policy.

## Source Selection

`SourceRouter` uses the structured intent result and deterministic policy rules. The LLM
may recognize source preferences, OA/full-text requirements, recency, and preprint
language, but it does not freely invent a source list.

```text
IntentRecognitionResult
-> SourceRouter
   -> SourceSelectionDecision
      -> selected primary sources
      -> allowed supplementary sources
      -> preprint policy
      -> clarification requirement
      -> selection reasons
-> SearchPlanBuilder
```

The source policy is:

| Source | Role | Activation |
|---|---|---|
| PubMed | Primary | Always for research searches |
| Europe PMC | Primary | Always for research searches |
| Semantic Scholar | Supplementary | LLM reports insufficient primary evidence |
| PMC Searcher | Supplementary | OA/full-text requested or insufficient primary evidence |
| PMC OA Resolver | Resource resolver | After deduplication for retained PMCIDs |
| bioRxiv | Preprint | `include_preprints` only |
| medRxiv | Preprint | `include_preprints` only |

An LLM recommendation cannot override the resolved preprint policy. The router validates
all requested sources against `allowed_supplementary_sources`.

## SearchPlan Extensions

The provider-neutral plan gains explicit scope, ordering, and time controls:

```python
class PublicationScope(StrEnum):
    PEER_REVIEWED_ONLY = "peer_reviewed_only"
    INCLUDE_PREPRINTS = "include_preprints"


class SearchSortMode(StrEnum):
    RELEVANCE = "relevance"
    NEWEST = "newest"


class PublicationDateRange(BaseModel):
    start_date: date | None = None
    end_date: date | None = None


class SourceSelectionDecision(BaseModel):
    publication_scope: PublicationScope | None
    primary_sources: list[SearchSource]
    allowed_supplementary_sources: list[SearchSource]
    requires_clarification: bool
    clarification_question: str | None
    reasons: list[str]
```

`SearchPlan` stores `publication_scope`, `sort_mode`, an optional date range, the selected
sources, and a fixed `max_results_per_source=100`. Pydantic rejects values above 100.

Provider compilers translate supported constraints into native syntax or request
parameters. When a provider cannot express a constraint, the normalized records are
validated and filtered locally. Unsupported filters are never silently dropped.

## Provider Connectors

### PubMed

PubMed remains an abstract and metadata source. ESearch supports relevance/date ordering
and date parameters; EFetch returns XML metadata and abstracts. It does not claim that a
downloadable full text exists.

### Europe PMC

Europe PMC remains a primary metadata/abstract source and can report OA/full-text
resources. It also acts as the keyword-discovery index for bioRxiv and medRxiv records.

### Semantic Scholar

The connector uses the official Academic Graph API. Normal supplementary discovery uses
`/graph/v1/paper/search`, which returns relevance-ranked results and accepts plain text,
date/year, publication type, field-of-study, and OA PDF filters.

Because the relevance endpoint does not accept Boolean syntax, its compiler selects
representative English terms from each SearchPlan concept group and produces a plain-text
query. When newest-first ordering is explicitly requested, the connector may use
`/paper/search/bulk` with `sort=publicationDate:desc`; that endpoint supports Boolean
matching but does not provide relevance ordering.

The API key is optional and loaded from configuration. The connector must handle lower
unauthenticated rate limits.

### bioRxiv And medRxiv

The official bioRxiv API supports lookup by date interval, category, or DOI, but it does
not provide general keyword search. The logical source connector therefore uses:

```text
Europe PMC preprint-constrained keyword discovery
-> identify candidate DOI and server
-> bounded official bioRxiv API DOI validation/enrichment
-> provider-specific result model
-> EvidenceRecord normalizer
```

Every normalized record from these connectors has
`peer_review_status="preprint"` and an explicit `preprint_server`. Official API version
and posted-date metadata are retained as provider data. Failure to validate the server
does not convert a record into peer-reviewed evidence.

### PMC

PMC is split into two units:

- `PMCSearcher` performs supplementary keyword discovery through NCBI ESearch with
  `db=pmc`, followed by bounded metadata/abstract retrieval.
- `PMCOAResolver` accepts a PMCID and calls the PMC OA Web Service. It returns the
  source-reported license, retraction flag, and available package links and formats.

The OA Service is not treated as a keyword search API. It is called after deduplication
and only for retained candidates, preventing hundreds of duplicate link-resolution
requests. PubMed and PMC remain separate sources even though they share NCBI
infrastructure.

## Unified Contracts

Each connector preserves a provider-specific validated response model. A dedicated
normalizer converts it to the provider-neutral contract.

```python
class PeerReviewStatus(StrEnum):
    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    UNKNOWN = "unknown"


class EvidenceRecord(BaseModel):
    source: SearchSource
    source_record_id: str
    search_run_id: str
    doi: str | None
    pmid: str | None
    pmcid: str | None
    title: str
    abstract: str | None
    authors: list[EvidenceAuthor]
    first_author: str | None
    journal_title: str | None
    journal_abbreviation: str | None
    publication_date: str | None
    publication_year: int | None
    publication_types: list[str]
    study_design: str | None
    peer_review_status: PeerReviewStatus
    preprint_server: str | None
    is_open_access: bool | None
    has_full_text: bool
    full_text_resources: list[FullTextResourceCandidate]
    citation_count: int | None
    landing_url: str | None
    retrieved_at: datetime
```

Missing scalar values use `None`; missing collections use `[]`. Source-reported
publication types, inferred study design, peer-review status, and OA status remain
separate concepts. OA is never used as a quality signal.

The outer `UnifiedSearchResult` contains source, search-run ID, status, hit count,
retrieved count, page count, retry count, invalid-record count, latency, records, and a
sanitized error. Status values are `success_with_results`, `success_empty`,
`partial_success`, and `failed`.

Provider raw payloads are not embedded in `EvidenceRecord`. The search execution layer
writes them to `source_records.raw_payload` before Graph fan-in.

## Ordering And Date Rules

- Each source returns at most 100 records.
- The default sort mode is `relevance`.
- An explicit date range is applied as a provider request constraint when supported and
  is validated locally after normalization.
- An explicit request for latest/recent evidence uses `newest` ordering.
- If the user asks for latest evidence without a period, the workflow asks for a time
  window instead of inventing one.
- Provider relevance ranks and scores are not comparable across databases.
- Final cross-source ranking occurs after normalization, deduplication, and abstract
  relevance evaluation.

## LLM Sufficiency Evaluation

The normal expansion decision is semantic, not a hard count check.

```python
class SufficiencyDecision(BaseModel):
    is_sufficient: bool
    confidence: float
    coverage_gaps: list[str]
    recommended_sources: list[SearchSource]
    recommended_search_adjustments: list[str]
    short_reason: str
```

The evaluator receives the original query, structured intent, SearchPlan, per-source
counts, unique abstract count, preliminary relevance distribution, compact structured
summaries of high-relevance candidates, and allowed supplementary sources. It does not
receive all raw provider payloads or hundreds of complete abstracts.

The decision is Pydantic-validated. Recommended sources are intersected with the allowed
source set, and `peer_reviewed_only` always excludes bioRxiv and medRxiv.

The configurable values `min_unique_abstracts=20` and `min_high_relevance=5` are logged
as diagnostic signals. They are used for routing only when the LLM call fails or its
structured output remains invalid after the bounded retry.

## LangGraph Flow

```text
intent_node
-> source_router_node
   -> unresolved preprint preference: interrupt for user clarification
   -> resolved: continue
-> mesh_normalizer_node
-> search_plan_node
-> compile_queries_node
-> primary fan-out: PubMed + Europe PMC
-> provider normalization and persistence
-> primary fan-in and deduplication
-> preliminary abstract relevance evaluation
-> LLM sufficiency_router
   -> sufficient: final ranking
   -> insufficient: permitted supplementary fan-out
-> supplementary normalization and persistence
-> second fan-in, deduplication, and final ranking
```

`EvidenceState` stores structured decisions, search-run IDs, article IDs, and small
search summaries. It does not store raw result collections. It inherits the existing
LangGraph message reducer instead of redeclaring `messages` as `list[str]`.

Fan-in uses keyed/idempotent reducers rather than `operator.add`, so a retried node cannot
duplicate an article or search summary.

## Persistence

- One provider invocation creates one `search_runs` row.
- Original provider records are written to `source_records.raw_payload`.
- Normalized, deduplicated publications are inserted or linked in `articles`.
- Discovered OA and downloadable representations are written to
  `full_text_resources`.
- The persistence layer uses fixed SQLAlchemy repository methods and transactions; no
  text-to-SQL is involved.

The article schema must add peer-review/preprint provenance fields before preprint
records are persisted. Migration details belong in the implementation plan.

## Failure Policy

- One provider failure does not cancel successful sibling searches. The batch becomes
  partial success.
- If both primary sources fail, retrieval stops with a retryable task error.
- Invalid individual records are skipped and counted without failing an otherwise valid
  page.
- Timeouts, HTTP 429, and HTTP 5xx use finite exponential-backoff retries.
- API keys, complete medical queries, abstracts, and raw response bodies are excluded
  from operational logs.
- A failed LLM sufficiency call uses the documented numeric fallback and records that
  degradation explicitly.

## Monitoring, Validation, And Evaluation

Operational telemetry records source, run ID, selected policy, result status, counts,
latency, retry count, invalid-record count, normalization count, deduplication count,
LLM token usage, sufficiency confidence, and fallback usage.

Pydantic validates every request, provider response boundary, normalized record,
SearchPlan extension, and LLM sufficiency decision.

Verification is layered:

1. SourceRouter and compiler unit tests for preprint, source, sort, and date rules.
2. Mock HTTP tests for every connector, pagination, rate limits, partial failure, and the
   100-record cap.
3. Provider-model-to-EvidenceRecord contract tests.
4. Repository transaction and deduplication integration tests against PostgreSQL.
5. LangGraph tests for clarification interrupts, primary/supplementary fan-out,
   partial-source failure, sufficiency routing, fallback behavior, and idempotent fan-in.
6. A small offline evaluation set of sufficient and insufficient research questions to
   monitor unnecessary expansion and missed coverage gaps.

## Implementation Order

1. Add shared enums, SearchPlan extensions, unified evidence contracts, and database
   fields/migration.
2. Implement SourceRouter and clarification state.
3. Refactor existing PubMed and Europe PMC models through normalizers.
4. Implement Semantic Scholar Connector and compiler.
5. Implement PMC Searcher and PMC OA Resolver.
6. Implement bioRxiv/medRxiv discovery and official-API enrichment.
7. Add repositories and provider persistence services.
8. Implement primary fan-out/fan-in and deterministic deduplication.
9. Implement preliminary relevance evaluation and LLM Sufficiency Evaluator.
10. Implement conditional supplementary fan-out and final merge/ranking.

## Out Of Scope

- Downloading full-text files to local storage.
- Parsing JATS XML, PDF, EPUB, or HTML full text.
- Formal risk-of-bias or evidence-quality appraisal.
- Evidence synthesis and academic report generation.
- Learned reward models or reinforcement-learning ranking.
