# PostgreSQL Evidence Storage Design

## Scope

The first PostgreSQL schema persists research-task ownership, individual source-search
runs, raw provider records, canonical deduplicated articles, and discovered full-text
resources. It does not yet persist screening scores, downloaded files, extracted
full-text sections, users, or journal-ranking enrichment.

## Identity Model

- `task_id`: one backend-created research task.
- `search_run_id`: one provider-specific search execution within a task.
- `source_record_pk`: one locally persisted raw result from a search run.
- `source_record_id`: the provider's identifier for that raw result.
- `article_id`: one canonical article after cross-source normalization and deduplication.
- `resource_id`: one discovered full-text representation.

LangGraph may use the value of `task_id` as its checkpoint `thread_id`, but the domain
name remains `task_id`.

## Tables

### `tasks`

Owns the original user query and task lifecycle. The backend creates its UUID. An
optional frontend `client_request_id` provides idempotency and an optional
`conversation_id` groups multiple research tasks in one chat.

### `search_runs`

Represents one actual source invocation, including source, compiled query, status, and
retrieval counts. Supplementary searches and retries create new rows instead of
overwriting prior evidence.

### `articles`

Stores canonical normalized metadata. DOI, PMID, and PMCID are independently nullable;
records without any of them remain identifiable through linked source records. The
clean plain-text abstract is stored here for screening, while the original provider
representation remains in `source_records.raw_payload`.

### `source_records`

Stores one provider response record per search run. Repeating the same provider record
in a later search run creates another raw row for simple, reproducible auditing. Once
normalization succeeds, `article_id` links it to the canonical article.

### `full_text_resources`

Stores discovered HTTP(S) full-text candidates and their source-reported access
metadata. It does not assert that a download has completed. A later artifact table will
store local path, content type, hash, size, and download status.

## Context Boundary

Raw payloads and complete article collections never enter global LangGraph messages.
Screening nodes keep `article_id` values in State, query selected normalized fields just
before invocation, validate them with Pydantic, and create a temporary JSON context.

## Integrity Policy

- Foreign keys preserve task, search-run, article, source-record, and resource lineage.
- A raw record is unique within one search run.
- DOI, PMID, and PMCID use partial unique indexes when present.
- JSON author lists must be arrays.
- Preferred resources must be marked downloadable.
- Only one preferred full-text resource may exist per article.
- Deleting a task cascades through its search runs and raw records; canonical articles
  are not automatically deleted because they may be shared by other tasks.

## Operational Boundary

The initial DDL is the sole schema source. SQLAlchemy repositories and Alembic migrations
will be introduced when application reads and writes are implemented. At that point,
the baseline DDL should become the first migration rather than being maintained as a
second independent schema definition.
