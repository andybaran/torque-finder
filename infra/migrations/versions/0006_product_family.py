"""add product_family + brand facet to documents (#28)

Revision ID: 0006_product_family
Revises: 0005_drop_pdfs_pages

Pure, non-destructive DDL: two nullable columns on ``documents`` plus a btree
index on ``product_family`` for the product-aware retrieval boost. Nullable +
non-destructive so existing rows are untouched and the FAIL-SAFE default
(NULL = product-blind) holds until the backfill (``scripts/backfill_product_family``)
populates them.

No ``SET LOCAL`` guards needed (unlike 0003/0004): adding nullable columns and a
btree index does not depend on session timezone or maintenance_work_mem.

This revision applies on the user's next GATED deploy (alembic upgrade head). It
is NOT applied to production from here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_product_family"
down_revision = "0005_drop_pdfs_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("product_family", sa.String(), nullable=True))
    op.add_column("documents", sa.Column("brand", sa.String(), nullable=True))
    op.create_index("ix_documents_product_family", "documents", ["product_family"])


def downgrade() -> None:
    op.drop_index("ix_documents_product_family", table_name="documents")
    op.drop_column("documents", "brand")
    op.drop_column("documents", "product_family")
