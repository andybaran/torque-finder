"""Hybrid retrieval: keyword + vector search fused via Reciprocal Rank Fusion.

RRF is a rank-only fusion (raw scores from the two channels are not
comparable: ts_rank_cd is unbounded, cosine similarity is in [-1, 1]).
For each page that appears in either channel we sum ``1 / (k + rank)`` over
the channels it appears in, where ``rank`` is 1-indexed.

We over-fetch ``DEFAULT_TOP_K_PER_CHANNEL`` from each channel before fusion
so a page that's strong in one channel and weak in the other still has a
chance to win the fused ranking.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.config import Settings
from parts_lookup.domain.errors import RetrievalError
from parts_lookup.domain.models import Query, RetrievalSource, RetrievedPage
from parts_lookup.retrieval.embedder import VoyageEmbedder
from parts_lookup.retrieval.keyword import keyword_search
from parts_lookup.retrieval.vector import vector_search

RRF_K: int = 60
DEFAULT_TOP_K_PER_CHANNEL: int = 20


_LOAD_PAGES_SQL = text(
    """
    SELECT
        p.id        AS page_id,
        p.pdf_id    AS pdf_id,
        p.page_no   AS page_no,
        p.text      AS text,
        p.png_r2_key AS png_r2_key,
        d.filename  AS pdf_filename
    FROM pages AS p
    JOIN pdfs  AS d ON d.id = p.pdf_id
    WHERE p.id IN :page_ids
    """
).bindparams(bindparam("page_ids", expanding=True))


@dataclass(frozen=True)
class _FusedHit:
    page_id: int
    score: float


def _reciprocal_rank_fusion(
    *channels: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[_FusedHit]:
    """Fuse multiple ranked lists into one. Higher fused score = better."""
    fused: dict[int, float] = {}
    for channel in channels:
        for rank, (page_id, _raw_score) in enumerate(channel, start=1):
            fused[page_id] = fused.get(page_id, 0.0) + 1.0 / (k + rank)
    return sorted(
        (_FusedHit(page_id=pid, score=score) for pid, score in fused.items()),
        key=lambda h: h.score,
        reverse=True,
    )


async def _load_pages(
    session: AsyncSession,
    page_ids: list[int],
) -> dict[int, dict[str, object]]:
    if not page_ids:
        return {}
    try:
        result = await session.execute(_LOAD_PAGES_SQL, {"page_ids": page_ids})
    except Exception as exc:
        raise RetrievalError("Failed to load fused page rows") from exc

    rows: dict[int, dict[str, object]] = {}
    for row in result:
        rows[int(row.page_id)] = {
            "pdf_id": int(row.pdf_id),
            "pdf_filename": str(row.pdf_filename),
            "page_no": int(row.page_no),
            "text": str(row.text),
            "png_r2_key": str(row.png_r2_key),
        }
    return rows


async def hybrid_search(
    session: AsyncSession,
    embedder: VoyageEmbedder,
    query_text: str,
    top_k: int,
    *,
    per_channel_k: int = DEFAULT_TOP_K_PER_CHANNEL,
) -> list[RetrievedPage]:
    """Run keyword + vector sequentially on the shared session, fuse with RRF, hydrate winners.

    The two channels are *not* run via ``asyncio.gather``: SQLAlchemy's
    ``AsyncSession`` does not allow concurrent operations on a single
    session (it only owns one DB connection at a time). Channel latency at
    this scale is dominated by network RTT to Postgres which is fast on
    LAN, so sequential is fine; if it ever isn't, the right move is two
    sessions, not gathered ops on one.
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

    page_rows = await _load_pages(session, [hit.page_id for hit in fused])

    retrieved: list[RetrievedPage] = []
    for hit in fused:
        row = page_rows.get(hit.page_id)
        if row is None:
            continue
        retrieved.append(
            RetrievedPage(
                pdf_id=row["pdf_id"],  # type: ignore[arg-type]
                pdf_filename=row["pdf_filename"],  # type: ignore[arg-type]
                page_no=row["page_no"],  # type: ignore[arg-type]
                text=row["text"],  # type: ignore[arg-type]
                png_r2_key=row["png_r2_key"],  # type: ignore[arg-type]
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
    ) -> list[RetrievedPage]:
        return await hybrid_search(
            session=session,
            embedder=self._embedder,
            query_text=query.text,
            top_k=query.top_k,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> RetrievalService:
        return cls(embedder=VoyageEmbedder(settings))
