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


class ProductScope(_Frozen):
    """The product/brand identity of a query or a document, deterministically derived.

    Pure value object shared across the retrieval and extraction contexts (and,
    per #28, the indexing context's ``product_family`` facet). It is produced by
    the deterministic matcher in ``retrieval.product_match`` from free text — a
    document title/filename or a mechanic's question — and carries NO model
    judgment.

    ``family`` is the normalized product family (e.g. ``"pike"``, ``"zeb"``,
    ``"code"``); ``brand`` is the normalized brand (e.g. ``"rockshox"``,
    ``"sram"``, ``"box"`` — brand is filled even for out-of-corpus brands so the
    gate can recognize *that* a product was asked about). ``confidence`` is the
    lexical-match strength: ``0.0`` means no product/brand was identified at all,
    in which case the abstention gate degrades to a no-op (never a false
    mismatch). The matcher is FAIL-SAFE: below its confidence bar it returns
    ``family=None`` rather than guessing.
    """

    family: str | None = None
    brand: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def is_identified(self) -> bool:
        """True when this scope names a recognizable product or brand.

        The abstention gate only engages when the *asked* scope is identified;
        an unidentified query (no recognized family or brand) is a no-op so the
        guard never over-abstains on an under-specified question.
        """
        return self.family is not None or self.brand is not None


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

    ``product_family`` / ``brand`` are the #28 product facet, deterministically
    derived from the title at ingest (and backfilled for legacy rows) via
    ``retrieval.product_match.derive_facet``. Both are nullable and FAIL-SAFE:
    a title that does not confidently identify a product leaves them ``None``
    rather than guessing — a wrong family would mis-boost retrieval and trip the
    contamination guard on correct answers.
    """

    id: int
    source_type: SourceType
    title: str
    source_url: str
    source_ref: str
    created_at: datetime
    product_family: str | None = None
    brand: str | None = None


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
    """A retrieval hit from the unified chunks index. ``score`` is the fused score.

    ``product_family`` / ``brand`` are hydrated from the owning document's #28
    facet so the product-aware boost (``retrieval.hybrid``) can compare a hit's
    product against the asked query scope without a second query. Both nullable:
    a document with no confidently-derived product stays product-blind (the
    boost is a no-op for it), preserving today's recall.
    """

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
    product_family: str | None = None
    brand: str | None = None


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
    # Default 5 (#29): over-fetch so a named doc that lands just below an
    # identical-spec sibling still reaches extraction. See config.retrieval_top_k.
    top_k: int = Field(default=5, ge=1, le=10)


class Answer(_Frozen):
    """Structured extraction result from Claude.

    ``source_index`` is the 1-based index of the candidate (in the order they
    were supplied to the extractor) the model cited — the caller maps it back
    to the retrieval hit. Tool/torque strings preserve the manual's notation.

    ``abstained`` is set when the safety gate declines to answer because the
    queried product is not in the corpus (see #32). On abstention there is no
    genuine source, so ``source_index`` is ``None`` (the caller must NOT surface
    a wrong-product deep link), ``tool_size``/``torque`` are ``None``, and
    ``confidence`` is clamped low.
    """

    text: str
    tool_size: str | None = None
    torque: str | None = None
    source_index: int | None = Field(default=None, ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    abstained: bool = False


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
