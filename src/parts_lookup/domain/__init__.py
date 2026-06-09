"""Pure domain types and errors. No I/O, no framework imports."""

from parts_lookup.domain.errors import (
    ExtractionError,
    IngestionError,
    PartsLookupError,
    PdfNotFoundError,
    RetrievalError,
)
from parts_lookup.domain.models import (
    Answer,
    HtmlChunk,
    IndexedDocument,
    PageContent,
    ParsedPublication,
    PdfDocument,
    Query,
    RetrievalSource,
    RetrievedChunk,
    SourceType,
)

__all__ = [
    "Answer",
    "ExtractionError",
    "HtmlChunk",
    "IndexedDocument",
    "IngestionError",
    "PageContent",
    "ParsedPublication",
    "PartsLookupError",
    "PdfDocument",
    "PdfNotFoundError",
    "Query",
    "RetrievalError",
    "RetrievalSource",
    "RetrievedChunk",
    "SourceType",
]
