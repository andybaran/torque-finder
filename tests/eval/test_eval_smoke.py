"""End-to-end eval harness over the canonical ground truth.

This is an opt-in regression suite: ``pytest -m eval``. Each case makes a
real Voyage embed + Postgres hybrid retrieval + Claude vision call. Roughly
~$0.01/case at current pricing.

Skips at module collection time if any required env var is missing, so the
default ``pytest`` invocation never tries to spend money or hit a database.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth import GROUND_TRUTH, GroundTruthCase

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

# Skip if the implementation modules haven't been merged yet.
pytest.importorskip("parts_lookup.retrieval.hybrid")
pytest.importorskip("parts_lookup.extraction.claude_client")
pytest.importorskip("parts_lookup.indexing.session")


pytestmark = [pytest.mark.eval, pytest.mark.asyncio]


def _matches(actual: str | None, expected: str | None) -> bool:
    """Soft substring match; ``expected=None`` is always satisfied."""
    if expected is None:
        return True
    if actual is None:
        return False
    return expected.lower() in actual.lower()


@pytest.mark.parametrize("case", GROUND_TRUTH, ids=lambda c: c.case_id)
async def test_ground_truth_case(case: GroundTruthCase) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.config import Settings
    from parts_lookup.domain.models import Query
    from parts_lookup.extraction.claude_client import ClaudeExtractor, ExtractionCandidate
    from parts_lookup.retrieval.hybrid import RetrievalService

    settings = Settings()  # type: ignore[call-arg]

    database_url = os.environ["DATABASE_URL"]
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, echo=False)
    retrieval = RetrievalService.from_settings(settings)
    extractor = ClaudeExtractor(settings)

    try:
        async with AsyncSession(engine) as session:
            hits = await retrieval.search(session, Query(text=case.query, top_k=3))

        assert hits, f"retrieval returned no hits for {case.case_id!r}"

        # Materialise PNGs from R2 to send to Claude.
        # NOTE: kept inline to avoid a hard dependency on a particular R2 client
        # signature. If the assets layer exposes ``fetch_png(r2_key)`` -> bytes,
        # swap this to use it.
        from parts_lookup.assets.r2_client import R2Client  # type: ignore[import-not-found]

        r2 = R2Client.from_settings(settings)  # type: ignore[attr-defined]
        candidates = [
            ExtractionCandidate(
                pdf_id=h.pdf_id,
                page_no=h.page_no,
                png_bytes=await r2.fetch(h.png_r2_key),  # type: ignore[attr-defined]
            )
            for h in hits
        ]

        answer = await extractor.extract(case.query, candidates)
    finally:
        await engine.dispose()

    # Soft assertions — print details before failing so the eval log is useful.
    page_match = answer.source_page_no == case.page_no
    tool_match = _matches(answer.tool_size, case.tool_size)
    torque_match = _matches(answer.torque, case.torque)

    assert page_match, (
        f"[{case.case_id}] expected page {case.page_no}, got {answer.source_page_no}; "
        f"answer={answer.text!r}"
    )
    assert tool_match, (
        f"[{case.case_id}] tool_size {answer.tool_size!r} does not contain {case.tool_size!r}"
    )
    assert torque_match, (
        f"[{case.case_id}] torque {answer.torque!r} does not contain {case.torque!r}"
    )
