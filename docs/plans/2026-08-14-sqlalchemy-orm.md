# SQLAlchemy ORM Implementation Plan

## Scope

Mirror the baseline PostgreSQL schema in `database/schema.sql` with SQLAlchemy 2.0
typed declarative models. The DDL remains the schema authority until Alembic is added.

## Implementation

- `app/db/base.py`: shared `DeclarativeBase`.
- `app/db/models.py`: mappings for `tasks`, `search_runs`, `articles`,
  `source_records`, and `full_text_resources`, including constraints, indexes,
  relationships, PostgreSQL JSONB/ARRAY/UUID types, and the generated abstract flag.
- `app/db/session.py`: lazy PostgreSQL URL handling, psycopg 3 async engine creation,
  and transaction-scoped async sessions.
- `tests/test_orm_models.py`: metadata and relationship contract checks without requiring
  a live database.

## Boundary

ORM objects stay in the repository/data-access layer. LangGraph state should carry task,
search-run, article, and resource IDs plus small validated screening projections rather
than entire ORM objects or raw provider payloads.
