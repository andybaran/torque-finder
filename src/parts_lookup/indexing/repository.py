"""SQLAlchemy ORM models + async write/read repository for the indexing context.

All DB I/O for the indexing bounded context lives here. The unified store is
``documents`` + ``chunks`` (PDF pages and HTML blocks alike). Other contexts
call ``Repository`` methods and receive pure domain objects — ORM rows never
escape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import (
    IndexedDocument,
    MinedChunk,
    SourceType,
)
from parts_lookup.retrieval.product_match import derive_facet


class Base(DeclarativeBase):
    """Shared declarative base. Alembic's env.py imports Base.metadata."""


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    source_url: Mapped[str] = mapped_column(nullable=False)
    source_ref: Mapped[str] = mapped_column(unique=True, nullable=False)
    # #28 product facet — deterministically derived from `title` at ingest /
    # backfill via retrieval.product_match.derive_facet. Nullable + FAIL-SAFE:
    # a title that doesn't confidently identify a product stays NULL (never a
    # guess). The product-aware retrieval boost reads `product_family`; `brand`
    # is the weaker fallback for generic multi-product manuals.
    product_family: Mapped[str | None] = mapped_column(nullable=True)
    brand: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("source_type IN ('pdf', 'html')", name="ck_documents_source_type"),
        # Boost lookups filter/group by product_family; a btree index keeps the
        # facet scan cheap as the corpus grows (low-cardinality but worth it).
        Index("ix_documents_product_family", "product_family"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    # Generated column maintained by Postgres; never written from Python.
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    png_r2_key: Mapped[str | None] = mapped_column(nullable=True)
    anchor: Mapped[str | None] = mapped_column(nullable=True)
    parent_anchor: Mapped[str | None] = mapped_column(nullable=True)
    source_url: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_module", "document_id", "parent_anchor", "ordinal"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


def _document_row_to_domain(row: Document) -> IndexedDocument:
    return IndexedDocument(
        id=row.id,
        source_type=SourceType(row.source_type),
        title=row.title,
        source_url=row.source_url,
        source_ref=row.source_ref,
        created_at=row.created_at,
        product_family=row.product_family,
        brand=row.brand,
    )


class Repository:
    """Async data-access object for the indexing context.

    The session is injected (FastAPI dep, ingestion CLI, or tests) so this
    class never owns connection lifecycle — callers commit/rollback.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_document(
        self,
        *,
        source_type: SourceType,
        title: str,
        source_url: str,
        source_ref: str,
    ) -> IndexedDocument:
        """Insert a document, or refresh title/source_url if source_ref exists.

        ``source_ref`` (sha256 for PDFs, pub_id for HTML) is the dedupe key,
        so re-ingest is idempotent at the document level. The source_type of
        an existing document is immutable: a mismatch raises IngestionError
        rather than silently keeping the old type.

        The #28 product facet (``product_family``/``brand``) is derived from the
        title at write time via the shared ``derive_facet`` (FAIL-SAFE: NULL
        unless a product is confidently identified). It is re-derived on the
        refresh path too, so a corrected title updates the facet on re-ingest.
        """
        product_family, brand = derive_facet(title)

        existing = await self._get_document_orm_by_source_ref(source_ref)
        if existing is not None:
            if existing.source_type != source_type.value:
                raise IngestionError(
                    f"document source_ref={source_ref!r} already exists with "
                    f"source_type={existing.source_type!r}; refusing upsert as "
                    f"{source_type.value!r}"
                )
            existing.title = title
            existing.source_url = source_url
            existing.product_family = product_family
            existing.brand = brand
            await self._session.flush()
            return _document_row_to_domain(existing)

        row = Document(
            source_type=source_type.value,
            title=title,
            source_url=source_url,
            source_ref=source_ref,
            product_family=product_family,
            brand=brand,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _document_row_to_domain(row)

    async def get_document_by_source_ref(self, source_ref: str) -> IndexedDocument | None:
        row = await self._get_document_orm_by_source_ref(source_ref)
        return _document_row_to_domain(row) if row is not None else None

    async def list_documents(self) -> Sequence[IndexedDocument]:
        """All documents (read model), ordered by title — backfill/audit support.

        Read-only; used by the #28 backfill (``scripts/backfill_product_family``)
        to inspect every title and re-derive the facet, and by tests. Returns
        pure domain objects; ORM rows never escape.
        """
        result = await self._session.execute(select(Document).order_by(Document.title))
        return [_document_row_to_domain(row) for row in result.scalars().all()]

    async def set_product_facet(
        self, document_id: int, product_family: str | None, brand: str | None
    ) -> None:
        """Write the derived ``(product_family, brand)`` onto an existing document.

        The backfill's write step. Deliberately separate from ``upsert_document``
        so the one-off re-derivation never has to re-supply title/source_url. The
        caller commits; this only flushes (so it composes inside a transaction
        and is safe to roll back in tests).
        """
        row = await self._session.get(Document, document_id)
        if row is None:
            raise IngestionError(f"document id={document_id} not found for facet update")
        row.product_family = product_family
        row.brand = brand
        await self._session.flush()

    async def insert_chunk(
        self,
        *,
        document_id: int,
        ordinal: int,
        text: str,
        embedding: Sequence[float],
        source_url: str,
        png_r2_key: str | None = None,
        anchor: str | None = None,
        parent_anchor: str | None = None,
    ) -> int:
        """Insert one chunk row; returns the new chunk id."""
        row = Chunk(
            document_id=document_id,
            ordinal=ordinal,
            text=text,
            embedding=list(embedding),
            png_r2_key=png_r2_key,
            anchor=anchor,
            parent_anchor=parent_anchor,
            source_url=source_url,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def delete_chunks(self, document_id: int) -> int:
        """Drop all chunks for a document (re-ingest of a stale publication).

        Returns the number of chunk rows deleted (0 for a first ingest).
        """
        result = await self._session.execute(
            delete(Chunk).where(Chunk.document_id == document_id)
        )
        return result.rowcount

    async def scan_chunks_matching(
        self,
        pattern: str,
        *,
        exclude_title_ilike: str | None = None,
        per_document_limit: int = 4,
        limit: int = 200,
    ) -> Sequence[MinedChunk]:
        """Read-only scan for chunks whose text matches a POSIX regex.

        Maintenance support for the eval ground-truth miner (``tests/eval``):
        returns up to ``per_document_limit`` matching chunks per document (lowest
        ordinals first, for diversity), capped at ``limit`` total. Stays inside
        the indexing bounded context so the miner never touches asyncpg directly.

        ``pattern`` is a Postgres ``~*`` (case-insensitive POSIX) regex;
        ``exclude_title_ilike`` drops documents whose title ILIKE-matches it
        (e.g. ``'%catalog%'``).
        """
        ranked_query = (
            select(
                Chunk.id.label("chunk_id"),
                Chunk.document_id,
                Document.title,
                Document.source_type,
                Chunk.ordinal,
                Chunk.png_r2_key,
                Chunk.source_url,
                Chunk.text,
                func.row_number()
                .over(partition_by=Chunk.document_id, order_by=Chunk.ordinal)
                .label("rn"),
            )
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.text.op("~*")(pattern))
        )
        if exclude_title_ilike is not None:
            ranked_query = ranked_query.where(~Document.title.ilike(exclude_title_ilike))
        ranked = ranked_query.subquery()

        result = await self._session.execute(
            select(
                ranked.c.chunk_id,
                ranked.c.document_id,
                ranked.c.title,
                ranked.c.source_type,
                ranked.c.ordinal,
                ranked.c.png_r2_key,
                ranked.c.source_url,
                ranked.c.text,
            )
            .where(ranked.c.rn <= per_document_limit)
            .order_by(ranked.c.rn, ranked.c.document_id)
            .limit(limit)
        )
        return [
            MinedChunk(
                chunk_id=row.chunk_id,
                document_id=row.document_id,
                document_title=row.title,
                source_type=SourceType(row.source_type),
                ordinal=row.ordinal,
                has_png=row.png_r2_key is not None,
                source_url=row.source_url,
                text=row.text,
            )
            for row in result
        ]

    async def fetch_module_text(self, document_id: int, parent_anchor: str) -> str:
        """Reconstruct a module's full text from its sibling chunks, in order.

        Small-to-big extraction (spec §2.3): blocks are embedded individually,
        but Claude reads the whole owning module.

        Returns "" when no chunks match (document_id, parent_anchor) — callers
        must treat the empty string as "module missing", not as valid text.
        """
        result = await self._session.execute(
            select(Chunk.text)
            .where(Chunk.document_id == document_id, Chunk.parent_anchor == parent_anchor)
            .order_by(Chunk.ordinal)
        )
        return "\n\n".join(result.scalars().all())

    async def _get_document_orm_by_source_ref(self, source_ref: str) -> Document | None:
        result = await self._session.execute(
            select(Document).where(Document.source_ref == source_ref)
        )
        return result.scalar_one_or_none()
