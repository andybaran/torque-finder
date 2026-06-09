"""Orchestrates ingestion of one HTML publication: fetch, parse, embed, store.

Mirrors the PDF pipeline's shape: owns no state, touches no I/O directly —
the discovery Fetcher (cache/robots/politeness), Repository, registry, and
VoyageEmbedder are injected. Re-ingest of a 'stale' publication is safe:
the document row is upserted by pub_id and old chunks are deleted first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from parts_lookup.domain.errors import IngestionError
from parts_lookup.domain.models import (
    IndexedDocument,
    RegisteredPublication,
    SourceType,
)
from parts_lookup.ingestion.html_parser import parse_publication

if TYPE_CHECKING:
    from parts_lookup.discovery.fetcher import Fetcher
    from parts_lookup.discovery.registry import PublicationRegistry
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.embedder import VoyageEmbedder


class HtmlIngestionPipeline:
    """End-to-end ingestion of a single registered publication."""

    def __init__(
        self,
        *,
        repository: Repository,
        registry: PublicationRegistry,
        fetcher: Fetcher,
        embedder: VoyageEmbedder,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._fetcher = fetcher
        self._embedder = embedder

    async def ingest_publication(self, pub: RegisteredPublication) -> IndexedDocument:
        """Ingest one publication; returns the resulting document."""
        try:
            html = await self._fetcher.get(pub.source_url)
        except Exception as exc:
            raise IngestionError(f"fetch failed for publication {pub.pub_id}") from exc

        parsed = parse_publication(html, base_url=pub.source_url)
        if not parsed.chunks:
            raise IngestionError(f"no chunks parsed from publication {pub.pub_id}")

        try:
            embeddings = await self._embedder.embed_documents(
                [chunk.text for chunk in parsed.chunks]
            )
        except Exception as exc:
            raise IngestionError(f"embedding failed for publication {pub.pub_id}") from exc
        if len(embeddings) != len(parsed.chunks):
            raise IngestionError(
                f"embedder returned {len(embeddings)} vectors for "
                f"{len(parsed.chunks)} chunks"
            )

        document = await self._repo.upsert_document(
            source_type=SourceType.HTML,
            title=parsed.title or pub.title,
            source_url=pub.source_url,
            source_ref=pub.pub_id,
        )
        await self._repo.delete_chunks(document.id)
        for chunk, embedding in zip(parsed.chunks, embeddings, strict=True):
            await self._repo.insert_chunk(
                document_id=document.id,
                ordinal=chunk.ordinal,
                text=chunk.text,
                embedding=embedding,
                png_r2_key=None,
                anchor=chunk.anchor,
                parent_anchor=chunk.parent_anchor,
                source_url=chunk.source_url,
            )

        await self._registry.set_status(pub.pub_id, "ingested")
        return document
