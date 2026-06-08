"""Per-page PNG rendering via pypdfium2.

Ingestion-only dependency. PDFium is a fast, deterministic renderer with no
ML weight, which is what we want for the screenshot artefacts that ship in
each query response.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

from parts_lookup.domain.errors import IngestionError

_POINTS_PER_INCH = 72


def render_pages(path: Path, dpi: int = 200) -> Iterator[tuple[int, bytes]]:
    """Yield ``(page_no, png_bytes)`` for every page in ``path``.

    ``page_no`` is 1-indexed to match docling and the public API.
    Scale is computed as ``dpi / 72`` since PDF user-space is 72 DPI.
    """
    if not path.is_file():
        raise IngestionError(f"PDF not found: {path}")
    if dpi <= 0:
        raise IngestionError(f"dpi must be positive, got {dpi}")

    try:
        import pypdfium2 as pdfium
    except Exception as exc:  # pragma: no cover - import-time failure
        raise IngestionError("pypdfium2 is not installed") from exc

    scale = dpi / _POINTS_PER_INCH

    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as exc:
        raise IngestionError(f"pypdfium2 failed to open {path}") from exc

    try:
        for idx in range(len(pdf)):
            page = pdf[idx]
            try:
                pil_image = page.render(scale=scale).to_pil()
                buffer = io.BytesIO()
                pil_image.save(buffer, format="PNG")
                yield idx + 1, buffer.getvalue()
            except Exception as exc:
                raise IngestionError(
                    f"pypdfium2 failed to render page {idx + 1} of {path}"
                ) from exc
            finally:
                close_page = getattr(page, "close", None)
                if callable(close_page):
                    close_page()
    finally:
        close_pdf = getattr(pdf, "close", None)
        if callable(close_pdf):
            close_pdf()
