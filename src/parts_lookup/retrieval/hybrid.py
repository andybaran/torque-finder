"""Hybrid retrieval: keyword + vector search fused via Reciprocal Rank Fusion.

RRF is a rank-only fusion (raw scores from the two channels are not
comparable: ts_rank_cd is unbounded, cosine similarity is in [-1, 1]).
For each chunk that appears in either channel we sum ``1 / (k + rank)`` over
the channels it appears in, where ``rank`` is 1-indexed.

We over-fetch ``DEFAULT_TOP_K_PER_CHANNEL`` from each channel before fusion
so a chunk that's strong in one channel and weak in the other still has a
chance to win the fused ranking.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.config import Settings
from parts_lookup.domain.errors import RetrievalError
from parts_lookup.domain.models import (
    Query,
    RetrievalSource,
    RetrievedChunk,
    SourceType,
)
from parts_lookup.retrieval.embedder import VoyageEmbedder
from parts_lookup.retrieval.keyword import keyword_search
from parts_lookup.retrieval.vector import vector_search

RRF_K: int = 60
DEFAULT_TOP_K_PER_CHANNEL: int = 20


_LOAD_CHUNKS_SQL = text(
    """
    SELECT
        c.id            AS chunk_id,
        c.document_id   AS document_id,
        c.ordinal       AS ordinal,
        c.text          AS text,
        c.png_r2_key    AS png_r2_key,
        c.anchor        AS anchor,
        c.parent_anchor AS parent_anchor,
        c.source_url    AS source_url,
        d.source_type   AS source_type,
        d.title         AS document_title,
        d.source_url    AS document_source_url
    FROM chunks    AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.id IN :chunk_ids
    """
).bindparams(bindparam("chunk_ids", expanding=True))


@dataclass(frozen=True)
class _FusedHit:
    chunk_id: int
    score: float


def _reciprocal_rank_fusion(
    *channels: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[_FusedHit]:
    """Fuse multiple ranked lists into one. Higher fused score = better."""
    fused: dict[int, float] = {}
    for channel in channels:
        for rank, (chunk_id, _raw_score) in enumerate(channel, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(
        (_FusedHit(chunk_id=cid, score=score) for cid, score in fused.items()),
        key=lambda h: h.score,
        reverse=True,
    )


async def hybrid_search(
    session: AsyncSession,
    embedder: VoyageEmbedder,
    query_text: str,
    top_k: int,
    *,
    per_channel_k: int = DEFAULT_TOP_K_PER_CHANNEL,
) -> list[RetrievedChunk]:
    """Run keyword + vector sequentially on the shared session, fuse with RRF, hydrate winners.

    (Sequential on purpose: AsyncSession owns a single connection — see the
    original comment; unchanged behaviour, new unified-chunk shape.)
    """
    query_embedding = await embedder.embed_query(query_text)

    try:
        keyword_hits = await keyword_search(session, query_text, per_channel_k)
        vector_hits = await vector_search(session, query_embedding, per_channel_k)
    except RetrievalError:
        raise
    except Exception as exc:
        raise RetrievalError("Hybrid retrieval channel failed") from exc

    fused = _reciprocal_rank_fusion(keyword_hits, vector_hits)[:top_k]
    if not fused:
        return []

    try:
        result = await session.execute(
            _LOAD_CHUNKS_SQL, {"chunk_ids": [hit.chunk_id for hit in fused]}
        )
    except Exception as exc:
        raise RetrievalError("Failed to load fused chunk rows") from exc

    rows = {int(row.chunk_id): row for row in result}

    retrieved: list[RetrievedChunk] = []
    for hit in fused:
        row = rows.get(hit.chunk_id)
        if row is None:
            continue
        retrieved.append(
            RetrievedChunk(
                chunk_id=int(row.chunk_id),
                document_id=int(row.document_id),
                source_type=SourceType(row.source_type),
                document_title=str(row.document_title),
                document_source_url=str(row.document_source_url),
                ordinal=int(row.ordinal),
                text=str(row.text),
                png_r2_key=None if row.png_r2_key is None else str(row.png_r2_key),
                anchor=None if row.anchor is None else str(row.anchor),
                parent_anchor=None if row.parent_anchor is None else str(row.parent_anchor),
                source_url=str(row.source_url),
                score=hit.score,
                source=RetrievalSource.HYBRID,
            )
        )
    return retrieved


class RetrievalService:
    """Application-layer entry point for hybrid retrieval.

    The API layer constructs one of these at startup (with a configured
    :class:`VoyageEmbedder`) and calls :meth:`search` per request, passing
    in a request-scoped :class:`AsyncSession`.
    """

    def __init__(self, embedder: VoyageEmbedder) -> None:
        self._embedder = embedder

    async def search(
        self,
        session: AsyncSession,
        query: Query,
    ) -> list[RetrievedChunk]:
        return await hybrid_search(
            session=session,
            embedder=self._embedder,
            query_text=query.text,
            top_k=query.top_k,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalService:
        return cls(embedder=VoyageEmbedder(settings))
