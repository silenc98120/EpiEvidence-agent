from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "database" / "schema.sql"


def _schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def test_schema_creates_required_tables() -> None:
    sql = _schema_sql().casefold()

    for table_name in (
        "tasks",
        "search_runs",
        "articles",
        "source_records",
        "full_text_resources",
    ):
        assert f"create table if not exists {table_name}" in sql


def test_schema_preserves_raw_and_normalized_article_content() -> None:
    sql = _schema_sql().casefold()

    assert "raw_payload jsonb not null" in sql
    assert "abstract text" in sql
    assert "authors jsonb not null" in sql
    assert "publication_types text[]" in sql


def test_schema_defines_identity_and_lineage_constraints() -> None:
    sql = _schema_sql().casefold()

    assert "foreign key (task_id)" in sql
    assert "foreign key (search_run_id)" in sql
    assert "foreign key (article_id)" in sql
    assert "foreign key (source_record_pk, article_id)" in sql
    assert "unique (search_run_id, source_record_id)" in sql
    assert "uq_articles_doi_normalized" in sql
    assert "uq_articles_pmid" in sql
    assert "uq_articles_pmcid_normalized" in sql


def test_schema_constrains_full_text_resources_without_claiming_download() -> None:
    sql = _schema_sql().casefold()

    assert "is_downloadable boolean" in sql
    assert "is_preferred boolean not null" in sql
    assert "'pdf', 'epub', 'html', 'xml', 'text', 'other'" in sql
    assert "not is_preferred or is_downloadable is true" in sql


def test_schema_has_no_destructive_drop_table_statement() -> None:
    assert "drop table" not in _schema_sql().casefold()
