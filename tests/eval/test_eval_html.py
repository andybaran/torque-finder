"""HTML-source eval: answers must come from a Red AXS publication with a
working docs.sram.com#hash deep link (spec §8). Opt-in: ``pytest -m eval``."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env
from tests.eval.ground_truth_html import HTML_GROUND_TRUTH, HtmlGroundTruthCase

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

_DEEP_LINK_RE = re.compile(r"^https://docs\.sram\.com/.+#.+$")


def _matches(actual: str | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if actual is None:
        return False
    return expected.lower() in actual.lower()


@pytest.mark.parametrize("case", HTML_GROUND_TRUTH, ids=lambda c: c.case_id)
async def test_html_ground_truth_case(case: HtmlGroundTruthCase) -> None:
    from parts_lookup.domain.models import SourceType
    from tests.eval.test_eval_smoke import run_query

    _hits, answer, chosen = await run_query(case.query, top_k=5)

    assert chosen.source_type is SourceType.HTML, (
        f"[{case.case_id}] expected an HTML source, got {chosen.source_type}; "
        f"answer={answer.text!r}"
    )
    assert _DEEP_LINK_RE.match(chosen.source_url), (
        f"[{case.case_id}] source_url is not a docs.sram.com#hash deep link: "
        f"{chosen.source_url!r}"
    )
    assert _matches(answer.tool_size, case.tool_contains), (
        f"[{case.case_id}] tool_size {answer.tool_size!r} missing {case.tool_contains!r}"
    )
    assert _matches(answer.torque, case.torque_contains), (
        f"[{case.case_id}] torque {answer.torque!r} missing {case.torque_contains!r}"
    )

    # The deep link must actually resolve (fragmentless GET).
    page_url = chosen.source_url.split("#", 1)[0]
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "parts-lookup-eval/0.1"},
    ) as http:
        response = await http.get(page_url)
    assert response.status_code == 200, f"deep link target returned {response.status_code}"
