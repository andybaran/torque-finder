"""docling-backed PDF parser.

Ingestion-only dependency — never imported from the runtime ``api`` layer.
We hide the docling import inside the function body to keep it lazy: the
ingestion CLI is the only call site, and even module-load of docling pulls
in PyTorch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parts_lookup.domain.errors import IngestionError

# Cached DocumentConverter. Its constructor loads ~770 layout-model
# weights and (on macOS via PyTorch's multiprocessing) opens semaphores
# that don't all get released. Re-creating it per PDF burned ~1-2s/file
# and accumulated semaphore leaks until the process crashed with SIGTRAP
# (rc=133) on long batch runs. One converter shared across the run fixes
# both. Module-level cache is fine: the CLI is single-process, asyncio.
_CONVERTER: Any = None


def _get_converter() -> Any:
    global _CONVERTER
    if _CONVERTER is None:
        try:
            from docling.document_converter import DocumentConverter
        except Exception as exc:  # pragma: no cover - import-time failure
            raise IngestionError("docling is not installed") from exc
        _CONVERTER = DocumentConverter()
    return _CONVERTER


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """One page worth of extracted text. ``page_no`` is 1-indexed."""

    page_no: int
    text: str


def parse_pdf(path: Path) -> list[ParsedPage]:
    """Extract per-page text from a PDF.

    Uses ``docling.document_converter.DocumentConverter``. docling's API has
    moved between minor versions; we try several access patterns in order:

    1. ``result.document.pages`` — the per-page model object exposes ``text``
       or a ``markdown``-equivalent.
    2. Per-page markdown export via ``result.document.export_to_markdown(page_no=...)``.
    3. As a last resort, the whole-document markdown export, split on form
       feeds (``\\f``) which docling emits between pages in some versions.

    The goal is one ``ParsedPage`` per source page, in page order.
    """
    if not path.is_file():
        raise IngestionError(f"PDF not found: {path}")

    converter = _get_converter()

    try:
        result = converter.convert(str(path))
    except Exception as exc:
        raise IngestionError(f"docling failed to convert {path}") from exc

    document = getattr(result, "document", None)
    if document is None:
        raise IngestionError(f"docling produced no document for {path}")

    pages = _extract_pages(document)
    if not pages:
        raise IngestionError(f"docling produced no page text for {path}")
    return pages


def _extract_pages(document: Any) -> list[ParsedPage]:
    # Path 1: structured pages on the document.
    raw_pages = getattr(document, "pages", None)
    if raw_pages:
        items = list(raw_pages.values()) if isinstance(raw_pages, dict) else list(raw_pages)
        out: list[ParsedPage] = []
        for idx, page in enumerate(items, start=1):
            page_no = int(getattr(page, "page_no", None) or getattr(page, "page", None) or idx)
            text = _page_text(page, document, page_no)
            if text.strip():
                out.append(ParsedPage(page_no=page_no, text=text))
        if out:
            return out

    # Path 2/3: whole-document markdown, split on form feed if present.
    export = getattr(document, "export_to_markdown", None)
    if callable(export):
        try:
            md = export()
        except Exception as exc:
            raise IngestionError("docling export_to_markdown failed") from exc
        if isinstance(md, str) and md:
            chunks = md.split("\f") if "\f" in md else [md]
            return [
                ParsedPage(page_no=i, text=chunk)
                for i, chunk in enumerate(chunks, start=1)
                if chunk.strip()
            ]

    return []


def _page_text(page: Any, document: Any, page_no: int) -> str:
    # Try the obvious attributes first.
    for attr in ("text", "markdown", "content"):
        value = getattr(page, attr, None)
        if isinstance(value, str) and value.strip():
            return value

    # Some versions provide an export method on the page itself.
    for method_name in ("export_to_markdown", "export_to_text"):
        fn = getattr(page, method_name, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                value = None
            if isinstance(value, str) and value.strip():
                return value

    # Some versions take ``page_no=`` on the document export.
    doc_export = getattr(document, "export_to_markdown", None)
    if callable(doc_export):
        try:
            value = doc_export(page_no=page_no)
        except TypeError:
            value = None
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value

    return ""
