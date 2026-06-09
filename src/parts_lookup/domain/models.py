"""Pure domain types that flow across bounded contexts.

These are deliberately framework-light: Pydantic v2 ``BaseModel`` for validation
and JSON serialisation, but no I/O, no SQLAlchemy, no FastAPI imports. The
``api`` layer wraps them in HTTP-shaped schemas; the ``indexing`` layer maps
them to ORM rows; everything else just uses them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    """Common config for value objects: immutable, strict, JSON-friendly."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RetrievalSource(StrEnum):
    """Which retrieval channel surfaced a page."""

    KEYWORD = "keyword"
    VECTOR = "vector"
    HYBRID = "hybrid"


class PdfDocument(_Frozen):
    """A manufacturer PDF that has been ingested."""

    id: int
    filename: str
    sha256: str = Field(min_length=64, max_length=64)
    r2_key: str
    page_count: int = Field(gt=0)
    created_at: datetime


class PageContent(_Frozen):
    """A single page's textual content + asset pointer.

    ``png_r2_key`` is the storage key for the rendered page image; the assets
    layer turns it into a URL.
    """

    pdf_id: int
    page_no: int = Field(gt=0)
    text: str
    png_r2_key: str


class Query(_Frozen):
    """A natural-language question from a mechanic."""

    text: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=3, ge=1, le=10)


class RetrievedPage(_Frozen):
    """A retrieval hit. ``score`` is the fused score (higher = better)."""

    pdf_id: int
    pdf_filename: str
    page_no: int
    text: str
    png_r2_key: str
    score: float
    source: RetrievalSource


class Answer(_Frozen):
    """Structured extraction result from Claude.

    The optional fields are typed loosely on purpose: tool sizes vary
    ("5mm hex", "T25 Torx", "7-8mm"), and torque values come in multiple units
    ("11 N-m (97 in-lb)"). We surface the raw strings the model produced and
    let the API layer present them.
    """

    text: str
    tool_size: str | None = None
    torque: str | None = None
    source_pdf_id: int
    source_page_no: int
    confidence: float = Field(ge=0.0, le=1.0)


class PublicationRef(_Frozen):
    """A publication link found on a model page. ``pub_type`` ∈ {'', 'UM', 'SM', 'BM'}."""

    pub_id: str
    pub_type: str = ""
    source_url: str


class DiscoveredPublication(_Frozen):
    """Metadata for one publication, parsed from its embedded manual-data JSON."""

    pub_id: str
    pub_type: str = ""
    title: str = ""
    locale: str = ""
    source_url: str
    series: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    procedures: tuple[str, ...] = ()
    content_hash: str


class RegisteredPublication(_Frozen):
    """A publication as recorded in the discovery registry (read model).

    Carries the registry-only bookkeeping fields (``status`` and timestamps)
    that ``DiscoveredPublication`` lacks, so the registry can return a pure
    domain object instead of leaking ORM rows.
    """

    pub_id: str
    pub_type: str
    title: str
    locale: str
    source_url: str
    series: tuple[str, ...]
    models: tuple[str, ...]
    procedures: tuple[str, ...]
    referenced_by_models: tuple[str, ...]
    content_hash: str
    status: str
    discovered_at: datetime
    last_seen_at: datetime
