"""remove quality verification payload

Revision ID: b91f0a6c2d44
Revises: a6d41b2f8c10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b91f0a6c2d44"
down_revision: Union[str, Sequence[str], None] = "a6d41b2f8c10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("quality_evaluation_batches", "verification_payload")


def downgrade() -> None:
    op.add_column(
        "quality_evaluation_batches",
        sa.Column("verification_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
