"""convert pdfs/pages.created_at to timestamptz

Revision ID: 0003_timestamptz
Revises: 0002_publications

These columns are event instants written via now() (UTC on the server) but
were stored as naive `timestamp`. Convert to `timestamp with time zone`.

IMPORTANT: do NOT use an `AT TIME ZONE` USING clause — that forces a full
table rewrite, which on the large `pages` table (one 1024-dim vector per row)
exhausts the Railway instance's disk/shared memory. Postgres 12+ performs a
timestamp→timestamptz change as a metadata-only operation (no rewrite) when
the session timezone is UTC, because the on-disk representation is identical.
We set the timezone for the transaction and let the implicit cast run.
Tracks GitHub issue #1.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_timestamptz"
down_revision = "0002_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")
    for table in ("pdfs", "pages"):
        op.alter_column(
            table,
            "created_at",
            type_=sa.DateTime(timezone=True),
            existing_type=sa.DateTime(),
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")
    for table in ("pdfs", "pages"):
        op.alter_column(
            table,
            "created_at",
            type_=sa.DateTime(),
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
