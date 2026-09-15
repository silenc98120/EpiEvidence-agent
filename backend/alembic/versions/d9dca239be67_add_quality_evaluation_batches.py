"""add quality evaluation batches

Revision ID: d9dca239be67
Revises: 20c0fc9ee6ad
Create Date: 2026-09-12 17:26:39.427368

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd9dca239be67'
down_revision: Union[str, Sequence[str], None] = '20c0fc9ee6ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quality_evaluation_batches",
        sa.Column(
            "quality_evaluation_batch_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("study_type", sa.Text(), nullable=False),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "article_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column(
            "assessment_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "verification_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "score_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "retained_article_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::UUID[]"),
        ),
        sa.Column(
            "excluded_article_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("ARRAY[]::UUID[]"),
        ),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("TIMEZONE('utc', NOW())"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "task_id",
            "batch_id",
            name="uq_quality_evaluation_batches_task_batch",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_quality_evaluation_batches_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_quality_evaluation_batches_attempt_count",
        ),
        sa.CheckConstraint(
            "length(btrim(batch_id)) > 0",
            name="ck_quality_evaluation_batches_batch_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(group_id)) > 0",
            name="ck_quality_evaluation_batches_group_id_not_blank",
        ),
        sa.CheckConstraint(
            "length(btrim(study_type)) > 0",
            name="ck_quality_evaluation_batches_study_type_not_blank",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_quality_evaluation_batches_completion_time",
        ),
    )

    op.create_index(
        "ix_quality_evaluation_batches_task_status",
        "quality_evaluation_batches",
        ["task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_evaluation_batches_task_status",
        table_name="quality_evaluation_batches",
    )
    op.drop_table("quality_evaluation_batches")