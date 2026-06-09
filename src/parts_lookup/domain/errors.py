"""Domain-level exceptions. Each bounded context maps its failures into these."""

from __future__ import annotations


class PartsLookupError(Exception):
    """Base class. Catch this in the API layer to translate to HTTP errors."""


class PdfNotFoundError(PartsLookupError):
    """A PDF was referenced (by id, sha256, or path) but does not exist."""


class IngestionError(PartsLookupError):
    """Ingestion pipeline failed (parse, render, embed, or store)."""


class RetrievalError(PartsLookupError):
    """Retrieval failed (embedding service, DB query, or fusion)."""


class ExtractionError(PartsLookupError):
    """Claude extraction failed or returned malformed output."""


class DiscoveryError(PartsLookupError):
    """Discovery/crawl failed (fetch, parse, or registry write)."""
