"""drop legacy pdfs/pages — GATED on the eval suite passing (spec §7)

Revision ID: 0005_drop_pdfs_pages
Revises: 0004_documents_chunks

This revision is deliberately NOT reached by scripts/start.sh (pinned to
0004_documents_chunks). Apply it manually, once the thoughts.md ground-truth
eval is green through documents/chunks in production:

    set -a && . ./.env && set +a
    uv run alembic -c alembic.ini upgrade head

A red eval leaves both schemas intact; downgrade() rebuilds pdfs/pages from
documents/chunks (ids are regenerated, content preserved).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0005_drop_pdfs_pages"
down_revision = "0004_documents_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_pages_embedding_hnsw", table_name="pages")
    op.drop_index("ix_pages_tsvector", table_name="pages")
    op.drop_index("ix_pages_pdf_id", table_name="pages")
    op.drop_table("pages")
    op.drop_index("ix_pdfs_sha256", table_name="pdfs")
    op.drop_table("pdfs")


def downgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")

    op.create_table(
        "pdfs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("r2_key", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("sha256", name="uq_pdfs_sha256"),
    )
    op.create_index("ix_pdfs_sha256", "pdfs", ["sha256"], unique=True)

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
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("pdf_id", "page_no", name="uq_pages_pdf_page"),
    )

    # Repopulate from the unified store (pdf rows only). page_count uses
    # max(ordinal), not count(*): ordinal carries the original 1-based page_no,
    # so max(ordinal) reproduces the original page_count semantics even if a
    # page row were ever missing (count(*) would silently disagree).
    op.execute(
        """
        INSERT INTO pdfs (filename, sha256, r2_key, page_count, created_at)
        SELECT d.title, d.source_ref, d.source_url,
               (SELECT coalesce(max(c.ordinal), 0) FROM chunks c WHERE c.document_id = d.id),
               d.created_at
        FROM documents AS d
        WHERE d.source_type = 'pdf'
        ORDER BY d.id
        """
    )
    op.execute(
        """
        INSERT INTO pages (pdf_id, page_no, text, embedding, png_r2_key, created_at)
        SELECT f.id, c.ordinal, c.text, c.embedding, c.png_r2_key, c.created_at
        FROM chunks AS c
        JOIN documents AS d ON d.id = c.document_id AND d.source_type = 'pdf'
        JOIN pdfs AS f ON f.sha256 = d.source_ref
        """
    )

    op.create_index("ix_pages_pdf_id", "pages", ["pdf_id"])
    op.create_index("ix_pages_tsvector", "pages", ["tsvector"], postgresql_using="gin")
    op.create_index(
        "ix_pages_embedding_hnsw",
        "pages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
