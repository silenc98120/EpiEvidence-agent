"""Persist deterministic title/abstract evidence spans."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import MetaData, Table, select, update
from backend.app.core.evidence_spans import SEGMENTER_VERSION, build_evidence_spans

revision = "20261031_evidence_spans"
down_revision = "c73a41e80b92"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("articles", sa.Column("evidence_spans", postgresql.JSONB(), nullable=True))
    op.add_column("articles", sa.Column("evidence_span_version", sa.Text(), nullable=True))
    connection = op.get_bind()
    articles = Table("articles", MetaData(), autoload_with=connection)
    rows = connection.execute(select(articles.c.article_id, articles.c.title, articles.c.abstract).where(articles.c.evidence_spans.is_(None)))
    for row in rows:
        spans = build_evidence_spans(row.article_id, row.title, row.abstract)
        connection.execute(
            update(articles).where(articles.c.article_id == row.article_id).values(
                evidence_spans=spans, evidence_span_version=SEGMENTER_VERSION,
            ),
        )


def downgrade():
    op.drop_column("articles", "evidence_span_version")
    op.drop_column("articles", "evidence_spans")
