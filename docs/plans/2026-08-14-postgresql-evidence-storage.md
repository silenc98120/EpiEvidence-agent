# PostgreSQL Evidence Storage Implementation Plan

## Task 1: Create the baseline schema

Files:

- `database/schema.sql`
- `database/README.md`

Steps:

1. Create task and provider-specific search-run lifecycle tables.
2. Create the canonical article table with nullable external identifiers, normalized
   abstract metadata, JSON authors, publication types, timestamps, and partial indexes.
3. Create raw source-record storage with JSONB payloads and normalization status.
4. Create full-text resource discovery storage with format, access, preferred-resource,
   and lineage constraints.
5. Add foreign-key, lookup, uniqueness, and screening indexes.
6. Document initialization, reset safety, inspection, and expected table names.

## Task 2: Add contract verification

File:

- `tests/test_postgresql_schema.py`

Steps:

1. Verify all five tables and required identifier columns exist in the DDL.
2. Verify raw payloads use JSONB and normalized abstracts use TEXT.
3. Verify cross-table lineage foreign keys and partial identifier indexes exist.
4. Verify the schema contains no destructive `DROP TABLE` statement.
5. Run the focused test and complete project test suite.

## Commands

```powershell
uv run python -m pytest tests/test_postgresql_schema.py -v
uv run python -m pytest -v
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/schema.sql
```

The first two commands are local contract checks. The final command performs the real
PostgreSQL syntax and creation check when a database URL is configured.
