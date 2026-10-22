"""Durable index progress and worker lease; originals and passage IDs remain immutable."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("index_error", sa.String(500)))
    op.add_column("sources", sa.Column("indexed_passages", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sources", sa.Column("index_total", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sources", sa.Column("index_owner", sa.String(36)))
    op.add_column("sources", sa.Column("index_lease_until", sa.DateTime()))
    op.execute(
        "UPDATE sources SET index_total=(SELECT COUNT(*) FROM passages WHERE passages.source_id=sources.id)"
    )
    op.execute("UPDATE sources SET indexed_passages=index_total WHERE index_status='ready'")


def downgrade():
    for name in ("index_lease_until", "index_owner", "index_total", "indexed_passages", "index_error"):
        op.drop_column("sources", name)
