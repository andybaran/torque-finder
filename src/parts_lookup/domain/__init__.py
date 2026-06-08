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
    PageContent,
    PdfDocument,
    Query,
    RetrievalSource,
    RetrievedPage,
)

__all__ = [
    "Answer",
    "ExtractionError",
    "IngestionError",
    "PageContent",
    "PartsLookupError",
    "PdfDocument",
    "PdfNotFoundError",
    "Query",
    "RetrievalError",
    "RetrievalSource",
    "RetrievedPage",
]
