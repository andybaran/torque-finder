"""Source-grounded sampled eval (PAID, gated): broader regression coverage.

For each frozen sampled case (``ground_truth_sampled.py``, mined from the live
``chunks`` store), run the shared in-process retrieval->extraction path and
grade the answer deterministically with ``grading.py``: value correctness
(notation + unit equivalence) and provenance (the cited chunk documents the
value). Assert the AGGREGATE pass rate against the recorded baseline minus a
tolerance — a regression gate, not a flaky per-row gate.

DOUBLE-GATED so it never fires by accident in CI:
1. module-level skip if the live env vars are absent (same as the smoke suite);
2. ``require_live_eval()`` per test → skip unless ``PARTS_EVAL_LIVE=1``.

It costs real Voyage + Claude money (~$0.02/case). Recording/refreshing the
committed baseline is a separate, explicitly user-greenlit step (see
``parts-lookup-eval`` and ``baselines/round1_sampled.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import LIVE_ENV_VARS, missing_env, require_live_eval

_missing = missing_env(LIVE_ENV_VARS)
if _missing:
    pytest.skip(
        f"eval suite requires live env vars (missing: {', '.join(_missing)})",
        allow_module_level=True,
    )

from tests.eval.grading import provenance_ok, value_matches  # noqa: E402
from tests.eval.ground_truth_sampled import SAMPLED_GROUND_TRUTH  # noqa: E402
from tests.eval.metrics import GradedRecord, aggregate, metrics_as_dict  # noqa: E402
from tests.eval.test_eval_smoke import run_query  # noqa: E402

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

_BASELINE = Path(__file__).resolve().parent / "baselines" / "round1_sampled.json"


async def _grade_one(case) -> GradedRecord:  # type: ignore[no-untyped-def]
    _hits, answer, chosen = await run_query(case.query)
    haystack = " | ".join(p for p in (answer.text, answer.torque) if p)
    value_ok = value_matches(haystack, case.expected_torque)
    prov_ok = provenance_ok(chosen.text, case.expected_torque)
    passed = value_ok and prov_ok
    return GradedRecord(
        case_id=case.case_id,
        passed=passed,
        failure_mode="correct" if passed else ("wrong_value" if not value_ok else "other"),
        retrieved_expected=prov_ok,
    )


async def test_sampled_pass_rate_meets_baseline() -> None:
    require_live_eval()

    records = [await _grade_one(case) for case in SAMPLED_GROUND_TRUTH]
    metrics = aggregate(records)

    baseline = json.loads(_BASELINE.read_text())
    frozen = baseline["frozen_sampled"]
    tolerance = float(frozen.get("tolerance", 0.10))

    print(
        f"\n[sampled] {metrics.n_pass}/{metrics.n} pass "
        f"({metrics.pass_rate:.1%}); histogram={metrics.failure_mode_histogram}"
    )

    if frozen.get("pass_rate") is None:
        pytest.skip(
            "frozen_sampled baseline not yet recorded; run "
            "`PARTS_EVAL_LIVE=1 parts-lookup-eval` to populate "
            f"(this run: pass_rate={metrics.pass_rate:.3f}, n={metrics.n})"
        )

    floor = float(frozen["pass_rate"]) - tolerance
    assert metrics.pass_rate >= floor, (
        f"sampled pass rate {metrics.pass_rate:.3f} below baseline "
        f"{frozen['pass_rate']:.3f} - tol {tolerance} = {floor:.3f}; "
        f"current metrics={metrics_as_dict(metrics)}"
    )
