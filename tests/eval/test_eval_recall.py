"""Recall@k regression gate for issue #29 (PAID, gated).

For each of the eight #29 evidence cases (``ground_truth_recall.py``), run the
shared retrieval path and check — *independent of extraction* — whether a chunk
that documents the expected value, within the named document family, appears in
the top-k retrieved hits. Reports recall@3 and recall@5 and gates recall@5 at
the issue's target of >= 6/8.

This is the fitness function for the Stage 1-3 retrieval fix: it measures
whether the named doc reaches the candidate set, not whether Claude answers
(that residual is #28/#32's abstain-policy territory). It deliberately makes NO
Claude call — only the Voyage embed + Postgres hybrid retrieval — so it is the
cheapest paid suite, but it still hits the live DB + Voyage, so it is gated like
the rest of the eval.

DOUBLE-GATED so it never fires by accident in CI:
1. module-level skip if the live env vars are absent;
2. ``require_live_eval()`` per test -> skip unless ``PARTS_EVAL_LIVE=1``.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env, require_live_eval

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"recall eval requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

from tests.eval.grading import normalize  # noqa: E402
from tests.eval.ground_truth_recall import RECALL_GROUND_TRUTH, RecallCase  # noqa: E402
from tests.eval.metrics import recall_at_k  # noqa: E402

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

# Issue #29 acceptance target: recall@5 on the recall suite >= 6/8.
_RECALL_AT_5_TARGET = 6


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _value_present(chunk_text: str, case: RecallCase) -> bool:
    """Does this chunk document the expected value (any accepted spelling)?"""
    haystack = normalize(chunk_text)
    candidates = (case.recall_value, *case.alt_recall_values)
    return any(normalize(v) in haystack for v in candidates)


def _within_family(document_title: str, case: RecallCase) -> bool:
    """Is this hit inside the named manual family (all family tokens present)?"""
    title = normalize(document_title)
    return all(tok in title for tok in case.family_tokens)


def _case_recalled(hits, case: RecallCase, *, k: int) -> bool:  # type: ignore[no-untyped-def]
    """Does a top-k hit document the value AND belong to the named family?"""
    for hit in hits[:k]:
        if _value_present(hit.text, case) and _within_family(hit.document_title, case):
            return True
    return False


async def _retrieve(question: str, top_k: int):  # type: ignore[no-untyped-def]
    """Voyage embed + Postgres hybrid retrieval only — no Claude call."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from parts_lookup.config import Settings
    from parts_lookup.domain.models import Query
    from parts_lookup.retrieval.hybrid import RetrievalService

    settings = Settings()  # type: ignore[call-arg]
    engine = create_async_engine(_database_url(), echo=False)
    retrieval = RetrievalService.from_settings(settings)
    try:
        async with AsyncSession(engine) as session:
            return await retrieval.search(session, Query(text=question, top_k=top_k))
    finally:
        await engine.dispose()


async def test_recall_at_5_meets_issue_target() -> None:
    require_live_eval()

    # Retrieve once at the deeper depth (5) and grade recall at both 3 and 5
    # from the same hit list.
    recalled_at_3: dict[str, bool] = {}
    recalled_at_5: dict[str, bool] = {}
    for case in RECALL_GROUND_TRUTH:
        hits = await _retrieve(case.query, top_k=5)
        recalled_at_3[case.case_id] = _case_recalled(hits, case, k=3)
        recalled_at_5[case.case_id] = _case_recalled(hits, case, k=5)

    m3 = recall_at_k(recalled_at_3, k=3)
    m5 = recall_at_k(recalled_at_5, k=5)

    print(
        f"\n[recall] recall@3 = {m3.n_recalled}/{m3.n} ({m3.recall_at_k:.1%}); "
        f"recall@5 = {m5.n_recalled}/{m5.n} ({m5.recall_at_k:.1%}); "
        f"missed@5={m5.missed_case_ids}"
    )

    assert m5.n_recalled >= _RECALL_AT_5_TARGET, (
        f"recall@5 {m5.n_recalled}/{m5.n} below issue #29 target "
        f">= {_RECALL_AT_5_TARGET}/8; missed: {m5.missed_case_ids}"
    )
