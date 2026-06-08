"""initial schema: pdfs, pages (+ pgvector, GIN tsvector, HNSW embedding)

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-25 00:00:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector must exist before any Vector column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "pdfs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("r2_key", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sha256", name="uq_pdfs_sha256"),
    )
    op.create_index("ix_pdfs_sha256", "pdfs", ["sha256"], unique=True)

    # tsvector is a generated column populated by Postgres on insert/update.
    # SQLAlchemy's Computed() emits the right DDL on Postgres 12+.
    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pdf_id",
            sa.Integer(),
            sa.ForeignKey("pdfs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsvector",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("png_r2_key", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("pdf_id", "page_no", name="uq_pages_pdf_page"),
    )

    op.create_index("ix_pages_pdf_id", "pages", ["pdf_id"])
    op.create_index(
        "ix_pages_tsvector",
        "pages",
        ["tsvector"],
        postgresql_using="gin",
    )
    # HNSW with cosine distance; m/ef_construction left at pgvector defaults
    # (m=16, ef_construction=64) — fine for ~1k PDFs / ~tens of thousands of
    # pages. Revisit if recall@k drops on the thoughts.md eval set.
    op.create_index(
        "ix_pages_embedding_hnsw",
        "pages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_pages_embedding_hnsw", table_name="pages")
    op.drop_index("ix_pages_tsvector", table_name="pages")
    op.drop_index("ix_pages_pdf_id", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_pdfs_sha256", table_name="pdfs")
    op.drop_table("pdfs")
    op.execute("DROP EXTENSION IF EXISTS vector")
