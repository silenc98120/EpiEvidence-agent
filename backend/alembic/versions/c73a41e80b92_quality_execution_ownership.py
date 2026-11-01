"""质量评估输入快照、租约所有权与尝试指标。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c73a41e80b92"
down_revision = "b91f0a6c2d44"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("quality_evaluation_batches", "celery_task_id")
    for name in ("execution_id", "attempt_id"):
        op.add_column("quality_evaluation_batches", sa.Column(name, postgresql.UUID(as_uuid=True)))
    op.add_column("quality_evaluation_batches", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    for name in ("input_payload", "metrics_payload"):
        op.add_column("quality_evaluation_batches", sa.Column(name, postgresql.JSONB()))
    op.drop_constraint("ck_quality_evaluation_batches_status", "quality_evaluation_batches")
    op.create_check_constraint("ck_quality_evaluation_batches_status", "quality_evaluation_batches",
        "status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')")
    op.create_index("ix_quality_batches_execution", "quality_evaluation_batches", ["task_id", "execution_id"])


def downgrade():
    op.add_column("quality_evaluation_batches", sa.Column("celery_task_id", sa.Text(), nullable=True))
    op.execute("UPDATE quality_evaluation_batches SET status='failed' WHERE status='cancelled'")
    op.drop_index("ix_quality_batches_execution", table_name="quality_evaluation_batches")
    op.drop_constraint("ck_quality_evaluation_batches_status", "quality_evaluation_batches")
    op.create_check_constraint("ck_quality_evaluation_batches_status", "quality_evaluation_batches",
        "status IN ('pending', 'processing', 'completed', 'failed')")
    for name in ("metrics_payload", "input_payload", "lease_expires_at", "attempt_id", "execution_id"):
        op.drop_column("quality_evaluation_batches", name)
