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


class SourceType(StrEnum):
    """Where indexed content came from: a manufacturer PDF or a digital (HTML) manual."""

    PDF = "pdf"
    HTML = "html"


class IndexedDocument(_Frozen):
    """A source document in the unified store (one PDF or one HTML publication).

    ``source_ref`` is the stable dedupe key: the PDF's sha256 or the
    publication's pub_id. For PDFs, ``source_url`` holds the R2 object key of
    the original file (the API resolves it to a public/presigned URL); for
    HTML it is the docs.sram.com publication URL.
    """

    id: int
    source_type: SourceType
    title: str
    source_url: str
    source_ref: str
    created_at: datetime


class HtmlChunk(_Frozen):
    """One block-level chunk parsed from a publication's manual-data JSON.

    ``anchor`` is the block's own #hash (not every block has one);
    ``parent_anchor`` is the owning module's #hash. ``source_url`` already
    resolves to the most precise hash available.
    """

    ordinal: int = Field(gt=0)
    text: str = Field(min_length=1)
    anchor: str | None = None
    parent_anchor: str | None = None
    source_url: str


class ParsedPublication(_Frozen):
    """Pure parser output for one publication: title + ordered chunks."""

    title: str
    chunks: tuple[HtmlChunk, ...]


class RetrievedChunk(_Frozen):
    """A retrieval hit from the unified chunks index. ``score`` is the fused score."""

    chunk_id: int
    document_id: int
    source_type: SourceType
    document_title: str
    document_source_url: str
    ordinal: int
    text: str
    png_r2_key: str | None
    anchor: str | None
    parent_anchor: str | None
    source_url: str
    score: float
    source: RetrievalSource


class MinedChunk(_Frozen):
    """A chunk surfaced by a maintenance text-pattern scan of the index.

    Read model for the eval ground-truth miner (``tests/eval``): just enough of
    a chunk to build a frozen ground-truth case — identity, the owning
    document, where it sits, and the raw text the value is mined from. Carries
    no embedding (the miner does not retrieve, it scans).
    """

    chunk_id: int
    document_id: int
    document_title: str
    source_type: SourceType
    ordinal: int
    has_png: bool
    source_url: str
    text: str


class Query(_Frozen):
    """A natural-language question from a mechanic."""

    text: str = Field(min_length=1, max_length=2_000)
    top_k: int = Field(default=3, ge=1, le=10)


class Answer(_Frozen):
    """Structured extraction result from Claude.

    ``source_index`` is the 1-based index of the candidate (in the order they
    were supplied to the extractor) the model cited — the caller maps it back
    to the retrieval hit. Tool/torque strings preserve the manual's notation.
    """

    text: str
    tool_size: str | None = None
    torque: str | None = None
    source_index: int = Field(ge=1)
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
