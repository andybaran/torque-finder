"""Adversarial out-of-corpus eval (PAID, gated): hallucination rate.

Runs the 39 frozen probes (``probes_out_of_corpus.py``) through the shared
retrieval->extraction path. Correct behavior on every probe is to ABSTAIN
(return no specific torque / low confidence) — or, for ``wrong_premise`` traps,
to correct the planted false number rather than confirm it. The metric is the
hallucination rate (share of probes that returned a confident specific torque
for a non-corpus target); asserted ≤ baseline + tolerance.

DOUBLE-GATED (module-level env skip + ``require_live_eval`` flag), same as the
sampled suite — never fires in CI, costs real money per probe.
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

from tests.eval.grading import is_abstention  # noqa: E402
from tests.eval.metrics import ProbeRecord, aggregate_adversarial  # noqa: E402
from tests.eval.probes_out_of_corpus import OUT_OF_CORPUS_PROBES  # noqa: E402
from tests.eval.test_eval_smoke import run_query  # noqa: E402

pytestmark = [pytest.mark.eval, pytest.mark.asyncio]

_BASELINE = Path(__file__).resolve().parent / "baselines" / "round2_adversarial.json"


async def _grade_probe(probe) -> ProbeRecord:  # type: ignore[no-untyped-def]
    _hits, answer, _chosen = await run_query(probe.question)
    abstained = is_abstention(answer.torque, answer.confidence)
    return ProbeRecord(
        probe_id=probe.probe_id,
        ptype=probe.ptype,
        hallucinated=not abstained,
    )


async def test_adversarial_hallucination_rate_within_baseline() -> None:
    require_live_eval()

    records = [await _grade_probe(probe) for probe in OUT_OF_CORPUS_PROBES]
    metrics = aggregate_adversarial(records)

    baseline = json.loads(_BASELINE.read_text())
    headline = baseline["headline"]
    tolerance = float(headline.get("tolerance", 0.05))
    ceiling = float(headline["hallucination_rate"]) + tolerance

    print(
        f"\n[adversarial] hallucination {metrics.n_hallucinated}/{metrics.n_probes} "
        f"({metrics.hallucination_rate:.1%}); by_ptype={metrics.by_ptype}"
    )

    assert metrics.hallucination_rate <= ceiling, (
        f"hallucination rate {metrics.hallucination_rate:.3f} above baseline "
        f"{headline['hallucination_rate']:.3f} + tol {tolerance} = {ceiling:.3f}; "
        f"by_ptype={metrics.by_ptype}"
    )
