"""SQLAlchemy ORM models + async write/read repository for the indexing context.

All DB I/O for the indexing bounded context lives here. Other contexts
(retrieval, ingestion, api) call ``Repository`` methods and receive pure
domain objects (`PdfDocument`, `PageContent`) — ORM rows never escape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from parts_lookup.domain.errors import PdfNotFoundError
from parts_lookup.domain.models import PageContent, PdfDocument


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
        nullable=False, server_default=func.now()
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
        nullable=False, server_default=func.now()
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

    async def _get_pdf_orm_by_sha256(self, sha256: str) -> Pdf | None:
        result = await self._session.execute(
            select(Pdf).where(Pdf.sha256 == sha256)
        )
        return result.scalar_one_or_none()
