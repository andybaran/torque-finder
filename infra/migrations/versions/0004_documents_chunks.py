"""create documents + chunks; copy pdfs/pages into them (set-based)

Revision ID: 0004_documents_chunks
Revises: 0003_timestamptz

Unified source-agnostic store (spec §3). The copy is INSERT … SELECT (no row
rewrites); the session timezone is pinned to UTC per the 0003 lesson (no
AT TIME ZONE USING clauses anywhere). The GIN/HNSW indexes are built AFTER
the bulk copy so the vector index is constructed in one pass instead of
per-row. The legacy tables are NOT touched here — dropping them is a
separate, eval-gated revision (spec §7).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0004_documents_chunks"
down_revision = "0003_timestamptz"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL timezone TO 'UTC'")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source_ref", name="uq_documents_source_ref"),
        sa.CheckConstraint(
            "source_type IN ('pdf', 'html')", name="ck_documents_source_type"
        ),
    )
    # NOTE: no separate ix_documents_source_ref index — the UniqueConstraint
    # above already creates a backing unique index; a second one is redundant.

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.Column("png_r2_key", sa.String(), nullable=True),
        sa.Column("anchor", sa.String(), nullable=True),
        sa.Column("parent_anchor", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
    )

    # --- set-based copy (pdf rows only exist today) ---
    op.execute(
        """
        INSERT INTO documents (source_type, title, source_url, source_ref, created_at)
        SELECT 'pdf', f.filename, f.r2_key, f.sha256, f.created_at
        FROM pdfs AS f
        ORDER BY f.id
        """
    )
    op.execute(
        """
        INSERT INTO chunks
            (document_id, ordinal, text, embedding, png_r2_key,
             anchor, parent_anchor, source_url, created_at)
        SELECT d.id, p.page_no, p.text, p.embedding, p.png_r2_key,
               NULL, NULL, f.r2_key || '#page=' || p.page_no, p.created_at
        FROM pages AS p
        JOIN pdfs AS f ON f.id = p.pdf_id
        JOIN documents AS d ON d.source_ref = f.sha256
        """
    )

    # --- indexes AFTER the copy ---
    # Railway lesson #2 (sibling of the 0003 UTC pin): the container caps
    # /dev/shm at 64 MB, and a *parallel* HNSW build allocates a dynamic
    # shared memory segment sized by maintenance_work_mem (64 MB) — the
    # resize fails with DiskFullError. Force a serial build: it uses
    # backend-local memory only, and ~9.8k 1024-dim vectors (~43 MB) fit
    # comfortably in maintenance_work_mem.
    op.execute("SET LOCAL max_parallel_maintenance_workers = 0")
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    # Module reconstruction: WHERE document_id = ? AND parent_anchor = ? ORDER BY ordinal
    op.create_index(
        "ix_chunks_module", "chunks", ["document_id", "parent_anchor", "ordinal"]
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index("ix_chunks_module", table_name="chunks")
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
