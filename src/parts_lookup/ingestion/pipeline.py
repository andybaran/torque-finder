"""Orchestrates ingestion of one PDF: hash, store, parse, render, embed, index.

Idempotency rule: a PDF is identified by its SHA-256. If a row with the same
hash already exists, the pipeline short-circuits and returns the existing
``PdfDocument`` — re-running ingest is safe and cheap.

The pipeline owns no state of its own and never touches the DB or HTTP
clients directly: every side effect goes through ``Repository``,
``R2Client``, or ``VoyageEmbedder``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import PdfDocument
from parts_lookup.ingestion.docling_parser import ParsedPage, parse_pdf
from parts_lookup.ingestion.rasterizer import render_pages

if TYPE_CHECKING:
    from parts_lookup.assets.r2_client import R2Client
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.embedder import VoyageEmbedder

_HASH_CHUNK = 1 << 20  # 1 MiB


class IngestionPipeline:
    """End-to-end ingestion of a single PDF."""

    def __init__(
        self,
        *,
        repository: Repository,
        r2_client: R2Client,
        embedder: VoyageEmbedder,
        render_dpi: int = 200,
    ) -> None:
        self._repo = repository
        self._r2 = r2_client
        self._embedder = embedder
        self._render_dpi = render_dpi

    async def ingest(self, pdf_path: Path) -> PdfDocument:
        """Ingest one PDF. Returns the resulting (or pre-existing) ``PdfDocument``."""
        path = Path(pdf_path)
        if not path.is_file():
            raise IngestionError(f"PDF not found: {path}")

        sha256 = await asyncio.to_thread(_sha256_file, path)

        existing = await self._repo.get_pdf_by_sha256(sha256)
        if existing is not None:
            return existing

        pdf_bytes = await asyncio.to_thread(path.read_bytes)
        pdf_key = f"pdfs/{sha256}.pdf"
        await self._r2.upload_bytes(pdf_key, pdf_bytes, "application/pdf")

        try:
            parsed_pages = await asyncio.to_thread(parse_pdf, path)
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"parsing failed for {path}") from exc

        if not parsed_pages:
            raise IngestionError(f"no pages parsed from {path}")

        text_by_page: dict[int, str] = {p.page_no: p.text for p in parsed_pages}
        page_count = max(text_by_page)

        pdf_doc = await self._repo.upsert_pdf(
            filename=path.name,
            sha256=sha256,
            r2_key=pdf_key,
            page_count=page_count,
        )

        # Embed all extracted page text. embed_documents() splits the inputs
        # into token-bounded batches internally to respect Voyage's per-request
        # limit, returning one vector per page in input order.
        embed_inputs = [_embed_input(p) for p in parsed_pages]
        try:
            embeddings = await self._embedder.embed_documents(embed_inputs)
        except Exception as exc:
            raise IngestionError(f"embedding failed for {path}") from exc
        if len(embeddings) != len(parsed_pages):
            raise IngestionError(
                f"embedder returned {len(embeddings)} vectors for "
                f"{len(parsed_pages)} pages"
            )
        embedding_by_page = {
            page.page_no: vec for page, vec in zip(parsed_pages, embeddings, strict=True)
        }

        await self._render_and_persist(
            path=path,
            pdf_doc=pdf_doc,
            sha256=sha256,
            text_by_page=text_by_page,
            embedding_by_page=embedding_by_page,
        )

        return pdf_doc

    async def _render_and_persist(
        self,
        *,
        path: Path,
        pdf_doc: PdfDocument,
        sha256: str,
        text_by_page: dict[int, str],
        embedding_by_page: dict[int, list[float]],
    ) -> None:
        # Hold the iterator in a thread so each `next()` runs on a worker —
        # keeps peak memory at one rendered page rather than the whole doc.
        try:
            pages_iter: Iterator[tuple[int, bytes]] = render_pages(
                path, dpi=self._render_dpi
            )
        except IngestionError:
            raise
        except Exception as exc:
            raise IngestionError(f"render init failed for {path}") from exc

        sentinel: tuple[int, bytes] | None = None
        while True:
            try:
                rendered = await asyncio.to_thread(next, pages_iter, sentinel)
            except IngestionError:
                raise
            except Exception as exc:
                raise IngestionError(f"render failed for {path}") from exc
            if rendered is None:
                break
            page_no, png_bytes = rendered

            text = text_by_page.get(page_no, "")
            embedding = embedding_by_page.get(page_no)
            if embedding is None:
                # Page rendered but had no parsed text. Skip silently — we
                # only index pages docling could read; rendering still gives
                # us a screenshot if some other page references this one.
                continue

            png_key = f"pages/{sha256}/{page_no:04d}.png"
            await self._r2.upload_bytes(png_key, png_bytes, "image/png")
            await self._repo.insert_page(
                pdf_id=pdf_doc.id,
                page_no=page_no,
                text=text,
                embedding=embedding,
                png_r2_key=png_key,
            )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _embed_input(page: ParsedPage) -> str:
    # Voyage rejects empty strings; fall back to a stable placeholder so the
    # ordering between inputs and outputs stays aligned.
    text = page.text.strip()
    return text if text else f"[empty page {page.page_no}]"
