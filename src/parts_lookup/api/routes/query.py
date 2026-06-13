"""POST /v1/query — the only real endpoint at v1.

Wires together the four downstream contexts (indexing, retrieval, assets,
extraction) into the single end-to-end mechanic-facing flow described in
CLAUDE.md. Candidates are source-agnostic: PDF chunks go to Claude as page
images; HTML chunks go as the parent module's text reconstructed from
sibling chunks (small-to-big).

Cost note (#29): the default candidate depth is ``top_k=5`` (up from 3).
The loop below fetches one PDF page PNG per retrieved candidate and sends
*all* of them to Claude vision, so on a PDF-heavy query 5 candidates is
``5/3 ≈ 1.67x`` the vision input tokens (and 5 sequential R2 PNG fetches vs
3) of the old depth — on *every* such query, not just the failing ones. At
~10 queries/day this stays inside the CLAUDE.md ~$10/mo Claude budget, but
the multiplier should be measured, not asserted: #34's eval harness captures
per-query cost so the budget claim stays honest.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, status

from parts_lookup.api.dependencies import (
    ExtractorDep,
    R2Dep,
    RetrievalDep,
    SessionDep,
)
from parts_lookup.api.schemas import AnswerResponse, Candidate, QueryRequest
from parts_lookup.assets.r2_client import R2Client
from parts_lookup.domain.errors import (
    ExtractionError,
    ExtractionUpstreamUnauthorized,
    ExtractionUpstreamUnavailable,
    RetrievalError,
)
from parts_lookup.domain.models import Query, RetrievedChunk, SourceType
from parts_lookup.extraction.claude_client import ExtractionCandidate
from parts_lookup.indexing.repository import Repository
from parts_lookup.observability import capture_exception as _capture_exception
from parts_lookup.observability import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["query"])

# Long-lived presigned URLs so a frontend can keep them on screen.
_PRESIGN_TTL_SECONDS = 60 * 60
_LABEL_MAX_CHARS = 80
# Retry-After (seconds) sent on a transient extraction 503 when the upstream
# response carried no Retry-After header of its own (e.g. a connection error).
_DEFAULT_RETRY_AFTER = "5"


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
            detail="No matching content found in the index.",
        )

    repo = Repository(session)
    candidates: list[ExtractionCandidate] = []
    # Sequential on purpose: repo.fetch_module_text shares this route's single
    # AsyncSession (no concurrent ops on one session), same constraint as hybrid.py.
    for index, hit in enumerate(retrieved, start=1):
        if hit.source_type is SourceType.PDF:
            if hit.png_r2_key is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"PDF chunk {hit.chunk_id} is missing its page PNG key.",
                )
            try:
                png = await _fetch_png(r2, hit.png_r2_key)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Asset store fetch failed.",
                ) from exc
            candidates.append(
                ExtractionCandidate(
                    index=index,
                    source_type=SourceType.PDF,
                    label=_pdf_label(hit),
                    png_bytes=png,
                )
            )
        else:
            module_text = await _module_text(repo, hit)
            candidates.append(
                ExtractionCandidate(
                    index=index,
                    source_type=SourceType.HTML,
                    label=_html_label(module_text, hit),
                    text=module_text,
                )
            )

    try:
        answer = await extractor.extract(body.question, candidates)
    # Order matters: both upstream classes subclass ExtractionError, so the
    # specific arms must precede the base arm. "Claude never answered"
    # (upstream) → 503; "Claude answered wrong" (parse/format/stop_reason,
    # #25's territory) → 502, unchanged.
    except ExtractionUpstreamUnavailable as exc:
        # Transient: tell the client to retry. Propagate the upstream
        # Retry-After when we have it, else a sane default.
        retry_after = exc.retry_after or _DEFAULT_RETRY_AFTER
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Answer service is temporarily unavailable; please retry.",
            headers={"Retry-After": retry_after},
        ) from exc
    except ExtractionUpstreamUnauthorized as exc:
        # Operator/code fault (billing/auth/permission/too-large): retrying
        # won't help, but it must page on-call. Capture to Sentry; keep the
        # client-facing detail generic (don't leak credit/auth specifics).
        _capture_exception(exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Answer service is temporarily unavailable.",
        ) from exc
    except ExtractionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Extraction failed: {exc}",
        ) from exc

    response_candidates: list[Candidate] = []
    for hit, candidate in zip(retrieved, candidates, strict=True):
        hit_source_url, hit_screenshot_url = await _source_links(r2, hit)
        response_candidates.append(
            Candidate(
                source_type=hit.source_type.value,
                label=candidate.label,
                score=hit.score,
                source_url=hit_source_url,
                screenshot_url=hit_screenshot_url,
            )
        )

    # The extractor guarantees source_index maps to a supplied candidate;
    # reuse that candidate's already-built links rather than recomputing them.
    chosen = retrieved[answer.source_index - 1]
    chosen_candidate = response_candidates[answer.source_index - 1]
    source_url = chosen_candidate.source_url
    screenshot_url = chosen_candidate.screenshot_url

    return AnswerResponse(
        answer=answer.text,
        tool_size=answer.tool_size,
        torque=answer.torque,
        confidence=answer.confidence,
        source_type=chosen.source_type.value,
        source_url=source_url,
        screenshot_url=screenshot_url,
        candidates=response_candidates,
    )


def _pdf_label(hit: RetrievedChunk) -> str:
    return f"p. {hit.ordinal} of {hit.document_title}"


def _html_label(module_text: str, hit: RetrievedChunk) -> str:
    """Module title = first line of the reconstructed module (the heading chunk)."""
    first_line = module_text.strip().split("\n", 1)[0].strip()
    label = first_line or hit.document_title
    return label[:_LABEL_MAX_CHARS]


async def _module_text(repo: Repository, hit: RetrievedChunk) -> str:
    """Small-to-big: hand Claude the whole owning module, not just the hit block."""
    if hit.parent_anchor is None:
        return hit.text
    text = await repo.fetch_module_text(hit.document_id, hit.parent_anchor)
    if not text:
        logger.warning(
            "query.module_text_missing",
            chunk_id=hit.chunk_id,
            parent_anchor=hit.parent_anchor,
        )
        return hit.text
    return text


async def _source_links(r2: R2Client, hit: RetrievedChunk) -> tuple[str, str | None]:
    """(source_url, screenshot_url) for one chunk.

    HTML chunks already carry a complete docs.sram.com#hash deep link and have
    no screenshot. PDF chunks store R2 *keys*; resolve them to URLs here.
    """
    if hit.source_type is SourceType.HTML:
        return hit.source_url, None
    pdf_url = await _resolve_url(r2, hit.document_source_url)
    screenshot = await _resolve_url(r2, hit.png_r2_key) if hit.png_r2_key else None
    return f"{pdf_url}#page={hit.ordinal}", screenshot


async def _fetch_png(r2: R2Client, key: str) -> bytes:
    """Read a PNG from R2 by key via a short-lived presigned GET URL."""
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
