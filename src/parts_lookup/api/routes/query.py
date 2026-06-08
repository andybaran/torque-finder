"""POST /v1/query — the only real endpoint at v1.

Wires together the four downstream contexts (indexing, retrieval, assets,
extraction) into the single end-to-end mechanic-facing flow described in
CLAUDE.md.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from parts_lookup.api.dependencies import (
    ExtractorDep,
    R2Dep,
    RetrievalDep,
    SessionDep,
)
from parts_lookup.api.schemas import AnswerResponse, CandidatePage, QueryRequest
from parts_lookup.assets.r2_client import R2Client
from parts_lookup.domain.errors import ExtractionError, RetrievalError
from parts_lookup.domain.models import Query
from parts_lookup.extraction.claude_client import ExtractionCandidate
from parts_lookup.indexing.repository import Pdf

router = APIRouter(prefix="/v1", tags=["query"])

# Long-lived presigned URLs so a frontend can keep them on screen.
_PRESIGN_TTL_SECONDS = 60 * 60


@router.post("/query", response_model=AnswerResponse)
async def query(
    body: QueryRequest,
    session: SessionDep,
    retrieval: RetrievalDep,
    extractor: ExtractorDep,
    r2: R2Dep,
) -> AnswerResponse:
    domain_query = Query(text=body.question, top_k=body.top_k)

    try:
        retrieved = await retrieval.search(session, domain_query)
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Retrieval failed: {exc}",
        ) from exc

    if not retrieved:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No matching pages found in the index.",
        )

    png_payloads = await asyncio.gather(
        *(_fetch_png(r2, hit.png_r2_key) for hit in retrieved)
    )

    candidates = [
        ExtractionCandidate(
            pdf_id=hit.pdf_id,
            page_no=hit.page_no,
            png_bytes=png,
        )
        for hit, png in zip(retrieved, png_payloads, strict=True)
    ]

    try:
        answer = await extractor.extract(body.question, candidates)
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Extraction failed: {exc}",
        ) from exc

    # Find the retrieval hit Claude pointed at (fall back to top hit).
    source_hit = next(
        (
            h
            for h in retrieved
            if h.pdf_id == answer.source_pdf_id and h.page_no == answer.source_page_no
        ),
        retrieved[0],
    )

    pdf_r2_key = await _lookup_pdf_r2_key(session, source_hit.pdf_id)
    source_png_url = await _resolve_url(r2, source_hit.png_r2_key)
    pdf_base_url = await _resolve_url(r2, pdf_r2_key)
    pdf_deep_link = f"{pdf_base_url}#page={source_hit.page_no}"

    candidate_urls = await asyncio.gather(
        *(_resolve_url(r2, hit.png_r2_key) for hit in retrieved)
    )

    return AnswerResponse(
        answer=answer.text,
        tool_size=answer.tool_size,
        torque=answer.torque,
        confidence=answer.confidence,
        source_page_no=answer.source_page_no,
        source_page_png_url=source_png_url,
        pdf_deep_link=pdf_deep_link,
        candidates=[
            CandidatePage(
                pdf_id=hit.pdf_id,
                pdf_filename=hit.pdf_filename,
                page_no=hit.page_no,
                score=hit.score,
                png_url=url,
            )
            for hit, url in zip(retrieved, candidate_urls, strict=True)
        ],
    )


async def _fetch_png(r2: R2Client, key: str) -> bytes:
    """Read a PNG from R2 by key via a short-lived presigned GET URL.

    Routing reads through httpx keeps R2Client small (no extra get_object
    surface) and means downloads benefit from httpx connection pooling.
    """
    url = await r2.generate_presigned_url(key, expires_in=300)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.content


async def _resolve_url(r2: R2Client, key: str) -> str:
    """Public URL when R2_PUBLIC_BASE_URL is set, else a presigned URL."""
    try:
        return r2.public_url(key)
    except Exception:
        return await r2.generate_presigned_url(key, expires_in=_PRESIGN_TTL_SECONDS)


async def _lookup_pdf_r2_key(session: AsyncSession, pdf_id: int) -> str:
    result = await session.execute(select(Pdf.r2_key).where(Pdf.id == pdf_id))
    r2_key = result.scalar_one_or_none()
    if r2_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF {pdf_id} referenced by retrieval hit is missing its R2 key.",
        )
    return str(r2_key)
