from sqlalchemy import inspect
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import configure_mappers

from app.db.base import Base
from app.db.models import (
    ArticleORM,
    FullTextResourceORM,
    SearchRunORM,
    SourceRecordORM,
    TaskORM,
)
from app.db.session import normalize_database_url


def test_orm_maps_all_baseline_tables_and_columns() -> None:
    expected_tables = {
        "tasks",
        "search_runs",
        "articles",
        "source_records",
        "full_text_resources",
    }
    assert set(Base.metadata.tables) == expected_tables

    assert {
        "task_id",
        "conversation_id",
        "client_request_id",
        "user_query",
        "status",
    } <= set(TaskORM.__table__.columns.keys())
    assert {"search_run_id", "task_id", "compiled_query", "status"} <= set(
        SearchRunORM.__table__.columns.keys()
    )
    assert {"article_id", "title", "abstract", "abstract_available", "authors"} <= set(
        ArticleORM.__table__.columns.keys()
    )
    assert {"source_record_pk", "search_run_id", "article_id", "raw_payload"} <= set(
        SourceRecordORM.__table__.columns.keys()
    )
    assert {"resource_id", "article_id", "source_record_pk", "resource_url", "format"} <= set(
        FullTextResourceORM.__table__.columns.keys()
    )


def test_orm_compiles_with_postgresql_dialect() -> None:
    statements = {
        table_name: str(CreateTable(table).compile(dialect=dialect()))
        for table_name, table in Base.metadata.tables.items()
    }

    assert set(statements) == {
        "tasks",
        "search_runs",
        "articles",
        "source_records",
        "full_text_resources",
    }
    assert "JSONB" in statements["source_records"]
    assert "ARRAY" in statements["articles"]
    assert "GENERATED ALWAYS AS" in statements["articles"]
    publication_index = next(
        index
        for index in ArticleORM.__table__.indexes
        if index.name == "ix_articles_publication_date"
    )
    assert "publication_date DESC NULLS LAST" in str(
        CreateIndex(publication_index).compile(dialect=dialect())
    )
    assert "FOREIGN KEY(source_record_pk, article_id)" in statements[
        "full_text_resources"
    ]


def test_orm_relationships_are_configured() -> None:
    configure_mappers()

    assert {relationship.key for relationship in inspect(TaskORM).relationships} == {
        "search_runs"
    }
    assert {relationship.key for relationship in inspect(SearchRunORM).relationships} == {
        "task",
        "source_records",
    }
    assert {relationship.key for relationship in inspect(ArticleORM).relationships} == {
        "source_records",
        "full_text_resources",
    }
    assert {relationship.key for relationship in inspect(SourceRecordORM).relationships} == {
        "search_run",
        "article",
        "full_text_resources",
    }
    assert {relationship.key for relationship in inspect(FullTextResourceORM).relationships} == {
        "article",
        "source_record",
    }


def test_postgresql_urls_use_psycopg_async_dialect() -> None:
    assert normalize_database_url("postgresql://localhost/epi") == (
        "postgresql+psycopg://localhost/epi"
    )
    assert normalize_database_url("postgresql+psycopg2://localhost/epi") == (
        "postgresql+psycopg://localhost/epi"
    )
