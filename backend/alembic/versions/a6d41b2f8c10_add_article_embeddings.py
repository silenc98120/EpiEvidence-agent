"""add article embeddings

Revision ID: a6d41b2f8c10
Revises: 357598264a0e
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a6d41b2f8c10"
down_revision: Union[str, Sequence[str], None] = "357598264a0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_embeddings",
        sa.Column("article_embedding_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("articles.article_id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("model_revision", sa.Text(), nullable=False),
        sa.Column("representation_version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("normalized", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("article_id", "model_name", "model_revision", "representation_version", "content_hash", name="uq_article_embeddings_identity"),
        sa.CheckConstraint("dimension > 0", name="ck_article_embeddings_dimension"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_article_embeddings_content_hash"),
    )
    op.create_index("ix_article_embeddings_article", "article_embeddings", ["article_id"])


def downgrade() -> None:
    op.drop_index("ix_article_embeddings_article", table_name="article_embeddings")
    op.drop_table("article_embeddings")
