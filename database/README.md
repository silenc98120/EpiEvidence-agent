# PostgreSQL Schema

`schema.sql` is the current baseline schema for EpiEvidence task, retrieval, canonical
article, raw-source, and full-text-resource data.

## Prerequisites

- PostgreSQL 12 or newer.
- An empty database and a role allowed to create tables and indexes.
- A `DATABASE_URL` using PostgreSQL connection syntax.

Example URL shape:

```text
postgresql://epi_user:password@localhost:5432/epi_evidence
```

Do not commit a real URL or password to the repository.

## Create the tables

PowerShell:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/schema.sql
```

`ON_ERROR_STOP=1` makes `psql` stop immediately and the surrounding transaction roll
back if any statement fails.

## Inspect the result

```powershell
psql "$env:DATABASE_URL" -c "\dt"
psql "$env:DATABASE_URL" -c "\d+ articles"
psql "$env:DATABASE_URL" -c "\d+ source_records"
psql "$env:DATABASE_URL" -c "\d+ full_text_resources"
```

Expected tables:

```text
tasks
search_runs
articles
source_records
full_text_resources
```

## Important boundary

The script intentionally contains no `DROP TABLE`. Resetting a database is destructive
and must be handled by a separately reviewed development-only command. Re-running this
baseline is safe for an unchanged schema because it uses `IF NOT EXISTS`; it is not a
replacement for future migrations.

## SQLAlchemy ORM

The application-side mappings are in `app/db/models.py` and use SQLAlchemy 2.0 typed
declarative models. They mirror this baseline schema and provide relationships for task,
search run, source record, article, and full-text resource objects.

`app/db/session.py` creates lazy async engines with psycopg 3:

```python
from app.db.session import build_session_factory, session_scope

session_factory = build_session_factory()

async with session_scope(session_factory) as session:
    # Use fixed ORM queries in a repository here.
    ...
```

Set `DATABASE_URL` to a PostgreSQL URL. The session module translates
`postgresql://...` to SQLAlchemy's `postgresql+psycopg://...` dialect. Importing the
module does not connect to PostgreSQL or create tables. Production schema changes remain
the responsibility of reviewed SQL/Alembic migrations; do not call
`Base.metadata.create_all` as an application startup migration.
