"""Pure domain types and errors. No I/O, no framework imports."""

from parts_lookup.domain.errors import (
    ExtractionError,
    IngestionError,
    PartsLookupError,
    RetrievalError,
)
from parts_lookup.domain.models import (
    Answer,
    HtmlChunk,
    IndexedDocument,
    ParsedPublication,
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
    "ParsedPublication",
    "PartsLookupError",
    "Query",
    "RetrievalError",
    "RetrievalSource",
    "RetrievedChunk",
    "SourceType",
]
