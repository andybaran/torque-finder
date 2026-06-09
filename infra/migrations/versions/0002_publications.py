"""publications registry table

Revision ID: 0002_publications
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0002_publications"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pub_id", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("pub_type", sa.String(), nullable=False, server_default=""),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("locale", sa.String(), nullable=False, server_default=""),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("series", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("models", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("procedures", ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "referenced_by_models",
            ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="discovered"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("publications")
