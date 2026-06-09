"""SQLAlchemy ORM models + async write/read repository for the indexing context.

All DB I/O for the indexing bounded context lives here. The unified store is
``documents`` + ``chunks`` (PDF pages and HTML blocks alike); the legacy
``pdfs``/``pages`` models remain only until the gated drop migration lands.
Other contexts call ``Repository`` methods and receive pure domain objects —
ORM rows never escape.
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

from parts_lookup.domain.errors import IngestionError, PdfNotFoundError
from parts_lookup.domain.models import (
    IndexedDocument,
    PageContent,
    PdfDocument,
    SourceType,
)


class Base(DeclarativeBase):
    """Shared declarative base. Alembic's env.py imports Base.metadata."""


class Pdf(Base):
    __tablename__ = "pdfs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(unique=True, nullable=False, index=True)
    r2_key: Mapped[str] = mapped_column(nullable=False)
    page_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    pages: Mapped[list[Page]] = relationship(
        back_populates="pdf",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Page(Base):
    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    pdf_id: Mapped[int] = mapped_column(
        ForeignKey("pdfs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_no: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(nullable=False)
    # Generated column maintained by Postgres; never written from Python.
    tsvector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    png_r2_key: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    pdf: Mapped[Pdf] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("pdf_id", "page_no", name="uq_pages_pdf_page"),
        Index("ix_pages_tsvector", "tsvector", postgresql_using="gin"),
        Index(
            "ix_pages_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    source_url: Mapped[str] = mapped_column(nullable=False)
    source_ref: Mapped[str] = mapped_column(unique=True, nullable=False)
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
    )


def _pdf_row_to_domain(row: Pdf) -> PdfDocument:
    return PdfDocument(
        id=row.id,
        filename=row.filename,
        sha256=row.sha256,
        r2_key=row.r2_key,
        page_count=row.page_count,
        created_at=row.created_at,
    )


def _page_row_to_domain(row: Page) -> PageContent:
    return PageContent(
        pdf_id=row.pdf_id,
        page_no=row.page_no,
        text=row.text,
        png_r2_key=row.png_r2_key,
    )


class Repository:
    """Async data-access object for the indexing context.

    The session is injected (FastAPI dep, ingestion CLI, or tests) so this
    class never owns connection lifecycle — callers commit/rollback.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_pdf(
        self,
        *,
        filename: str,
        sha256: str,
        r2_key: str,
        page_count: int,
    ) -> PdfDocument:
        """Insert a PDF, or return the existing row if its sha256 already exists.

        Dedupes re-uploads of the identical file without surprising the caller.
        """
        existing = await self._get_pdf_orm_by_sha256(sha256)
        if existing is not None:
            return _pdf_row_to_domain(existing)

        row = Pdf(
            filename=filename,
            sha256=sha256,
            r2_key=r2_key,
            page_count=page_count,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _pdf_row_to_domain(row)

    async def insert_page(
        self,
        *,
        pdf_id: int,
        page_no: int,
        text: str,
        embedding: Sequence[float],
        png_r2_key: str,
    ) -> PageContent:
        """Insert a single page row. Caller must pass a 1024-dim embedding."""
        row = Page(
            pdf_id=pdf_id,
            page_no=page_no,
            text=text,
            embedding=list(embedding),
            png_r2_key=png_r2_key,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _page_row_to_domain(row)

    async def get_pdf_by_sha256(self, sha256: str) -> PdfDocument | None:
        row = await self._get_pdf_orm_by_sha256(sha256)
        return _pdf_row_to_domain(row) if row is not None else None

    async def list_pdfs(self) -> list[PdfDocument]:
        result = await self._session.execute(select(Pdf).order_by(Pdf.id))
        return [_pdf_row_to_domain(row) for row in result.scalars().all()]

    async def get_page(self, pdf_id: int, page_no: int) -> PageContent:
        """Fetch a single page. Raises PdfNotFoundError if the (pdf, page) pair is unknown."""
        result = await self._session.execute(
            select(Page).where(Page.pdf_id == pdf_id, Page.page_no == page_no)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise PdfNotFoundError(
                f"page not found: pdf_id={pdf_id} page_no={page_no}"
            )
        return _page_row_to_domain(row)

    async def fetch_pages_by_ids(self, page_ids: Sequence[int]) -> list[PageContent]:
        """Hydrate retrieval hits (which carry page-row ids) into domain pages.

        Order of the returned list mirrors ``page_ids`` so callers can zip scores back.
        """
        if not page_ids:
            return []
        result = await self._session.execute(
            select(Page).where(Page.id.in_(list(page_ids)))
        )
        rows = {row.id: row for row in result.scalars().all()}
        return [_page_row_to_domain(rows[pid]) for pid in page_ids if pid in rows]

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
        """
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
            await self._session.flush()
            return _document_row_to_domain(existing)

        row = Document(
            source_type=source_type.value,
            title=title,
            source_url=source_url,
            source_ref=source_ref,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _document_row_to_domain(row)

    async def get_document_by_source_ref(self, source_ref: str) -> IndexedDocument | None:
        row = await self._get_document_orm_by_source_ref(source_ref)
        return _document_row_to_domain(row) if row is not None else None

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

    async def _get_pdf_orm_by_sha256(self, sha256: str) -> Pdf | None:
        result = await self._session.execute(
            select(Pdf).where(Pdf.sha256 == sha256)
        )
        return result.scalar_one_or_none()
