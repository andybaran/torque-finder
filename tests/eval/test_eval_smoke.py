"""End-to-end eval harness over the canonical thoughts.md ground truth.

Opt-in regression suite (``pytest -m eval``): each case makes a real Voyage
embed + Postgres hybrid retrieval over the unified ``chunks`` store + a real
Claude call. Roughly ~$0.01/case. This suite is the SAFETY GATE for the
pdfs/pages drop migration (spec §7): 0005 must not run until it is green.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth import GROUND_TRUTH, GroundTruthCase

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]


def _matches(actual: str | None, expected: str | None) -> bool:
    """Soft substring match; ``expected=None`` is always satisfied."""
    if expected is None:
        return True
    if actual is None:
        return False
    return expected.lower() in actual.lower()


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _fetch_png(r2, key: str) -> bytes:  # type: ignore[no-untyped-def]
    url = await r2.generate_presigned_url(key, expires_in=300)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.content


async def run_query(question: str, top_k: int = 3):  # type: ignore[no-untyped-def]
    """Shared retrieval→extraction runner. Returns (hits, answer, chosen_hit)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.assets.r2_client import R2Client
    from parts_lookup.config import Settings
    from parts_lookup.domain.models import Query, SourceType
    from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate
    from parts_lookup.indexing.repository import Repository
    from parts_lookup.retrieval.hybrid import RetrievalService

    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(_database_url(), echo=False)
    retrieval = RetrievalService.from_settings(settings)
    extractor = ClaudeExtractor(settings)
    r2 = R2Client(settings)

    try:
        async with AsyncSession(engine) as session:
            hits = await retrieval.search(session, Query(text=question, top_k=top_k))
            assert hits, f"retrieval returned no hits for {question!r}"

            candidates: list[ExtractionCandidate] = []
            repo = Repository(session)
            for index, hit in enumerate(hits, start=1):
                if hit.source_type is SourceType.PDF:
                    assert hit.png_r2_key is not None
                    candidates.append(
                        ExtractionCandidate(
                            index=index,
                            source_type=SourceType.PDF,
                            label=f"p. {hit.ordinal} of {hit.document_title}",
                            png_bytes=await _fetch_png(r2, hit.png_r2_key),
                        )
                    )
                else:
                    module_text = hit.text
                    if hit.parent_anchor is not None:
                        module_text = (
                            await repo.fetch_module_text(hit.document_id, hit.parent_anchor)
                            or hit.text
                        )
                    candidates.append(
                        ExtractionCandidate(
                            index=index,
                            source_type=SourceType.HTML,
                            label=module_text.split("\n", 1)[0][:80],
                            text=module_text,
                        )
                    )

        answer = await extractor.extract(question, candidates)
    finally:
        await engine.dispose()

    return hits, answer, hits[answer.source_index - 1]


@pytest.mark.parametrize("case", GROUND_TRUTH, ids=lambda c: c.case_id)
async def test_ground_truth_case(case: GroundTruthCase) -> None:
    from parts_lookup.domain.models import SourceType

    _hits, answer, chosen = await run_query(case.query)

    assert chosen.source_type is SourceType.PDF, (
        f"[{case.case_id}] expected a PDF source, got {chosen.source_type}"
    )
    assert chosen.ordinal == case.page_no, (
        f"[{case.case_id}] expected page {case.page_no}, got {chosen.ordinal}; "
        f"answer={answer.text!r}"
    )
    assert _matches(answer.tool_size, case.tool_size), (
        f"[{case.case_id}] tool_size {answer.tool_size!r} does not contain {case.tool_size!r}"
    )
    assert _matches(answer.torque, case.torque), (
        f"[{case.case_id}] torque {answer.torque!r} does not contain {case.torque!r}"
    )
