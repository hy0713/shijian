"""Initial immutable MySQL schema. Models are not imported here so future model changes do not alter this revision."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("author", sa.String(250)),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("chapter", sa.String(250)),
        sa.Column("edition", sa.String(500)),
        sa.Column("source_url", sa.Text()),
        sa.Column("raw_text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("content_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("people", sa.JSON(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("provenance_note", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(), nullable=False),
        sa.Column("index_status", sa.String(20), nullable=False),
        sa.Column("embedding_key", sa.String(500)),
    )
    op.create_index("ix_sources_index_status", "sources", ["index_status"])
    op.create_table(
        "source_locations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.UniqueConstraint("source_id", "url_hash"),
    )
    op.create_index("ix_source_locations_source_id", "source_locations", ["source_id"])
    op.create_table(
        "passages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start", sa.Integer(), nullable=False),
        sa.Column("end", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("prev_id", sa.String(36)),
        sa.Column("next_id", sa.String(36)),
        sa.Column("tokens_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("source_id", "ordinal"),
    )
    op.create_index("ix_passages_source_id", "passages", ["source_id"])
    op.create_table(
        "postings",
        sa.Column("token", sa.String(100), primary_key=True),
        sa.Column("passage_id", sa.String(36), sa.ForeignKey("passages.id"), primary_key=True),
        sa.Column("frequency", sa.Integer(), nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("owner", sa.String(36)),
        sa.Column("lease_until", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_task_status_lease", "tasks", ["status", "lease_until"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("time", sa.DateTime(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_table(
        "task_evidence",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("passage_id", sa.String(36), sa.ForeignKey("passages.id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )


def downgrade():
    for name in ("task_evidence", "events", "tasks", "postings", "passages", "source_locations", "sources"):
        op.drop_table(name)
