"""Opt-in snapshot indexing, collections and immutable research scopes."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None


def upgrade():
    # Existing indexed/demo material stays available; no originals or evidence IDs change.
    op.add_column("sources", sa.Column("saved", sa.Boolean(), nullable=False, server_default="1"))
    op.create_index("ix_sources_saved", "sources", ["saved"])
    op.add_column("tasks", sa.Column("source_ids", sa.JSON(), nullable=True))
    op.create_table(
        "collections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("people", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table, column, target in (
        ("collection_sources", "source_id", "sources.id"),
        ("collection_tasks", "task_id", "tasks.id"),
    ):
        op.create_table(
            table,
            sa.Column("collection_id", sa.String(36), sa.ForeignKey("collections.id"), primary_key=True),
            sa.Column(column, sa.String(36), sa.ForeignKey(target), primary_key=True),
        )


def downgrade():
    op.drop_table("collection_tasks")
    op.drop_table("collection_sources")
    op.drop_table("collections")
    op.drop_column("tasks", "source_ids")
    op.drop_index("ix_sources_saved", "sources")
    op.drop_column("sources", "saved")
