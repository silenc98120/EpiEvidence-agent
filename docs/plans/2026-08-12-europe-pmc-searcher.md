# Europe PMC Searcher Implementation Plan

## Scope

Implement the first Europe PMC online-search tool. It receives a query already compiled
by `EuropePMCQueryCompiler`, submits bounded asynchronous `core` searches, follows cursor
pagination, and normalizes source records into typed article metadata plus full-text
resource discovery fields.

This unit does not download, parse, rank, deduplicate, or appraise articles and does not
add LangGraph fan-out/fan-in orchestration.

## Task 1: Runtime contracts and record mapping

Files:

- `app/tools/article_search/europepmc.py`
- `pyproject.toml`

Steps:

1. Add `httpx` as a direct application dependency.
2. Define Pydantic request, raw response, normalized article, full-text resource, error,
   and search-result models.
3. Normalize source booleans, identifiers, authors, journal metadata, publication types,
   HTML-bearing abstracts, and full-text URLs.
4. Select the preferred resource deterministically with PDF first, followed by EPUB,
   HTML, XML, text, and other readable formats.
5. Treat missing text as `None`, missing collections as `[]`, and unknown booleans as
   `None`.

## Task 2: Asynchronous search and bounded pagination

File: `app/tools/article_search/europepmc.py`

Steps:

1. Submit `application/x-www-form-urlencoded` POST requests to `/searchPOST` with
   `resultType=core`, `format=json`, `synonym=false`, and cursor pagination.
2. Stop at `max_results`, an empty page, a missing/unchanged cursor, or `max_pages`.
3. Retry only timeouts, transport errors, HTTP 429, and HTTP 5xx with bounded backoff.
4. Return distinct success, empty, partial-success, and failed statuses.
5. Emit Loguru metrics without logging the medical query or abstracts.

## Task 3: Verification

Files:

- `tests/test_europepmc_searcher.py`
- `README.md`
- `docs/project-1-epi-evidence-agent-design.md`

Steps:

1. Exercise source mapping with a representative `core` record and verify PDF-first
   resource selection.
2. Exercise two-page cursor pagination with an injected mock HTTP transport.
3. Exercise empty and partial-failure outcomes.
4. Run the focused test module, then the complete test suite.
5. Update the project progress documents with exactly the implemented boundary.

## Expected commands

```powershell
uv lock
uv run python -m pytest tests/test_europepmc_searcher.py -v
uv run python -m pytest -v
```

All tests should pass. The default test suite must not require live network access.
