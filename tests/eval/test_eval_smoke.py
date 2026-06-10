"""End-to-end eval harness over the canonical thoughts.md ground truth.

Opt-in regression suite (``pytest -m eval``): each case makes a real Voyage
embed + Postgres hybrid retrieval over the unified ``chunks`` store + a real
Claude call. Roughly ~$0.01/case. This suite is the SAFETY GATE for the
pdfs/pages drop migration (spec §7): 0005 must not run until it is green.

This is a **value-pinned gate** (see ``ground_truth.py`` for full semantics):
every case asserts the pinned value, the citation's provenance, and a working
source link; only cases whose value is unique in the corpus additionally pin
document identity + page number.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth import (
    GROUND_TRUTH,
    GROUND_TRUTH_DOCUMENT_SHA256,
    GROUND_TRUTH_DOCUMENT_TITLE,
    GroundTruthCase,
)

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

# HTML citations must be docs.sram.com deep links with a #hash fragment.
_DEEP_LINK_RE = re.compile(r"^https://docs\.sram\.com/.+#.+$")


def _normalize(value: str) -> str:
    """Collapse notation variants so the same number+unit always compares equal.

    Only notation is normalized — the digits and units themselves must still
    match exactly:
    - newton-metre separators: ``N-m`` / ``N·m`` / ``N⋅m`` / ``Nm`` / ``N m``
      (the PDF prints ``N·m``; ``thoughts.md`` writes ``N-m``)
    - number/unit spacing: ``4 mm`` ≡ ``4mm``
    - numeric ranges: ``7 to 8`` / ``7 and 8`` / en- or em-dash ranges ≡ ``7-8``
    - Torx spelling: ``T-25`` ≡ ``T25``
    """
    normalized = value.lower()
    normalized = re.sub(r"(?<![a-z])n\s*[·⋅-]?\s*m(?![a-z])", "n-m", normalized)
    normalized = re.sub(r"(\d)\s+mm(?![a-z])", r"\1mm", normalized)
    dashes = "\u2013\u2014"  # en dash, em dash (ruff RUF001 bans the literals)
    normalized = re.sub(
        r"(\d(?:\.\d+)?)\s*(?:[-" + dashes + r"]|to|and)\s*(\d)", r"\1-\2", normalized
    )
    normalized = re.sub(r"(?<![a-z])t\s*-\s*(\d)", r"t\1", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _matches(actual: str | None, expected: str | None) -> bool:
    """Notation-normalized substring match; ``expected=None`` is always satisfied."""
    if expected is None:
        return True
    if actual is None:
        return False
    return _normalize(expected) in _normalize(actual)


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


def _case_params() -> list:  # type: ignore[type-arg]
    """Wrap each case in pytest.param so known-red cases carry their xfail mark."""
    params = []
    for case in GROUND_TRUTH:
        marks = (
            [pytest.mark.xfail(strict=False, reason=case.xfail_reason)]
            if case.xfail_reason is not None
            else []
        )
        params.append(pytest.param(case, id=case.case_id, marks=marks))
    return params


@pytest.mark.parametrize("case", _case_params())
async def test_ground_truth_case(case: GroundTruthCase) -> None:
    from parts_lookup.domain.models import SourceType

    _hits, answer, chosen = await run_query(case.query)

    # --- value gate: the pinned tool/torque must appear in the answer. -----
    # combined_fields cases match against all answer fields together, so
    # "Use a 5 mm hex key" in the text with tool_size="5 mm" still passes a
    # pinned "5mm hex".
    if case.combined_fields:
        combined = " | ".join(
            part for part in (answer.text, answer.tool_size, answer.torque) if part
        )
        tool_haystack: str | None = combined
        torque_haystack: str | None = combined
    else:
        tool_haystack = answer.tool_size
        torque_haystack = answer.torque

    if case.tool_size is not None:
        accepted_tools = (case.tool_size, *case.alt_tool_sizes)
        assert any(_matches(tool_haystack, tool) for tool in accepted_tools), (
            f"[{case.case_id}] tool {tool_haystack!r} matches none of {accepted_tools!r}"
        )
    assert _matches(torque_haystack, case.torque), (
        f"[{case.case_id}] torque {torque_haystack!r} does not contain {case.torque!r}"
    )

    # --- provenance gate: the citation must be genuine. --------------------
    # Either the chosen hit's chunk text documents the pinned value, or the
    # hit is a figure page verified (against its rendered PNG) to show the
    # value in its callouts — docling's text extraction of figure callouts is
    # lossy, so page identity stands in for text containment there. The
    # ground-truth page itself is always in the verified set.
    pinned_value = case.torque or case.tool_size
    assert pinned_value is not None, f"[{case.case_id}] case pins no value"
    verified_figure_pages = {
        (GROUND_TRUTH_DOCUMENT_SHA256, case.page_no),
        *case.verified_figure_sources,
    }
    is_verified_figure_page = any(
        sha256 in chosen.document_source_url and chosen.ordinal == page_no
        for sha256, page_no in verified_figure_pages
    )
    assert _normalize(pinned_value) in _normalize(chosen.text) or is_verified_figure_page, (
        f"[{case.case_id}] cited source ({chosen.document_title!r}, ordinal "
        f"{chosen.ordinal}) does not document {pinned_value!r}; chunk text: "
        f"{chosen.text[:200]!r}"
    )

    # --- source-link gate: the response must carry a working deep link. ----
    if chosen.source_type is SourceType.PDF:
        assert chosen.source_url.endswith(f"#page={chosen.ordinal}"), (
            f"[{case.case_id}] PDF source_url {chosen.source_url!r} lacks a "
            f"#page={chosen.ordinal} fragment"
        )
        # run_query already fetched this page's PNG via a presigned GET, so a
        # non-None key here means the screenshot demonstrably resolves.
        assert chosen.png_r2_key is not None, (
            f"[{case.case_id}] PDF hit has no page screenshot"
        )
    else:
        assert _DEEP_LINK_RE.match(chosen.source_url), (
            f"[{case.case_id}] source_url is not a docs.sram.com#hash deep link: "
            f"{chosen.source_url!r}"
        )
        # The deep link must actually resolve (fragmentless GET).
        page_url = chosen.source_url.split("#", 1)[0]
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "parts-lookup-eval/0.1"},
        ) as http:
            response = await http.get(page_url)
        assert response.status_code == 200, (
            f"[{case.case_id}] deep link target returned {response.status_code}"
        )

    # --- strict document pin: only where the value is unique in the corpus.
    # "Any manual's page N" must not pass; the sha256 is pinned via the
    # document's R2 key, which embeds the ingestion dedupe content hash.
    if case.strict:
        assert chosen.source_type is SourceType.PDF, (
            f"[{case.case_id}] expected a PDF source, got {chosen.source_type}"
        )
        assert chosen.document_title == GROUND_TRUTH_DOCUMENT_TITLE, (
            f"[{case.case_id}] expected document {GROUND_TRUTH_DOCUMENT_TITLE!r}, "
            f"got {chosen.document_title!r} (page {chosen.ordinal})"
        )
        assert GROUND_TRUTH_DOCUMENT_SHA256 in chosen.document_source_url, (
            f"[{case.case_id}] document key {chosen.document_source_url!r} does not "
            f"carry the ground-truth sha256"
        )
        assert chosen.ordinal == case.page_no, (
            f"[{case.case_id}] expected page {case.page_no}, got {chosen.ordinal}; "
            f"answer={answer.text!r}"
        )
