"""One command for the quality eval gate (issue #34).

    uv run python -m tests.eval.run_eval

Prints the full metric set the issue asks for — pass rate, recall@k,
contamination %, hallucination %, tool-size completeness — plus the deltas
against the recorded baselines.

PAID and gated. The source-grounded + adversarial suites hit real Voyage +
Claude (~$0.02/case, ~$2.5 a full run; a run once drained the credit balance),
so this refuses to make any paid call unless ``PARTS_EVAL_LIVE=1`` is set. With
the flag absent it runs in DRY mode: prints the recorded baselines, the frozen
corpus sizes, and the estimated cost, and exits 0 without spending anything.

    # offline / dry — $0, what CI and a casual check do:
    uv run python -m tests.eval.run_eval

    # paid live run (explicit opt-in, source ./.env first):
    set -a && . ./.env && set +a
    PARTS_EVAL_LIVE=1 uv run python -m tests.eval.run_eval

Recording/refreshing the committed baselines from a fresh run is a further,
separately user-greenlit step (a paid run whose printed numbers are pasted into
``baselines/*.json``) — never done automatically.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# This module lives under tests/ (not shipped in the wheel). When run as
# ``python -m tests.eval.run_eval`` the repo root is already on sys.path, but
# keep this guard so it also works if launched by path. Import the ``tests.*``
# packages only after the root is guaranteed present (hence the E402 noqas).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.conftest import LIVE_ENV_VARS, missing_env  # noqa: E402
from tests.eval.grading import is_abstention, provenance_ok, value_matches  # noqa: E402
from tests.eval.ground_truth_sampled import SAMPLED_GROUND_TRUTH  # noqa: E402
from tests.eval.metrics import (  # noqa: E402
    GradedRecord,
    ProbeRecord,
    aggregate,
    aggregate_adversarial,
    compare_to_baseline,
)
from tests.eval.probes_out_of_corpus import OUT_OF_CORPUS_PROBES  # noqa: E402

_FLAG = "PARTS_EVAL_LIVE"
_BASELINE_DIR = Path(__file__).resolve().parent / "baselines"
_COST_PER_QUERY = 0.02  # USD, sonnet-4-6 + 3 page images


def _n_paid_calls() -> int:
    return len(SAMPLED_GROUND_TRUTH) + len(OUT_OF_CORPUS_PROBES)


def _estimated_cost() -> float:
    return _n_paid_calls() * _COST_PER_QUERY


def _print_dry() -> None:
    print("=" * 70)
    print("parts-lookup-eval — DRY RUN (no paid calls made)")
    print("=" * 70)
    print(f"sampled cases:        {len(SAMPLED_GROUND_TRUTH)}")
    print(f"adversarial probes:   {len(OUT_OF_CORPUS_PROBES)}")
    print(f"paid /v1/query calls: {_n_paid_calls()}")
    print(f"estimated cost:       ~${_estimated_cost():.2f} (at ${_COST_PER_QUERY}/query)")
    print()
    for name in ("round1_sampled.json", "round2_adversarial.json"):
        data = json.loads((_BASELINE_DIR / name).read_text())
        print(f"--- recorded baseline: {name} ---")
        print(json.dumps(data, indent=1)[:1200])
        print()
    missing = missing_env(LIVE_ENV_VARS)
    print(
        f"To run live: set {_FLAG}=1 and source ./.env "
        + ("(env OK)" if not missing else f"(missing: {', '.join(missing)})")
    )


async def _run_sampled() -> list[GradedRecord]:
    from tests.eval.test_eval_smoke import run_query

    records: list[GradedRecord] = []
    for case in SAMPLED_GROUND_TRUTH:
        _hits, answer, chosen = await run_query(case.query)
        haystack = " | ".join(p for p in (answer.text, answer.torque) if p)
        value_ok = value_matches(haystack, case.expected_torque)
        prov_ok = provenance_ok(chosen.text, case.expected_torque)
        passed = value_ok and prov_ok
        records.append(
            GradedRecord(
                case_id=case.case_id,
                passed=passed,
                failure_mode="correct"
                if passed
                else ("wrong_value" if not value_ok else "other"),
                retrieved_expected=prov_ok,
            )
        )
    return records


async def _run_adversarial() -> list[ProbeRecord]:
    from tests.eval.test_eval_smoke import run_query

    records: list[ProbeRecord] = []
    for probe in OUT_OF_CORPUS_PROBES:
        _hits, answer, _chosen = await run_query(probe.question)
        records.append(
            ProbeRecord(
                probe_id=probe.probe_id,
                ptype=probe.ptype,
                hallucinated=not is_abstention(answer.torque, answer.confidence),
            )
        )
    return records


def _print_metrics(sampled, adversarial) -> None:  # type: ignore[no-untyped-def]
    sm = aggregate(sampled)
    am = aggregate_adversarial(adversarial)
    print("=" * 70)
    print("QUALITY EVAL METRICS")
    print("=" * 70)
    print(f"source-grounded pass rate: {sm.n_pass}/{sm.n} ({sm.pass_rate:.1%})")
    print(f"recall@k:                  {sm.recall_at_k:.1%}")
    print(f"contamination %:           {sm.contamination_pct:.1f}%")
    print(f"tool-size completeness:    {sm.tool_size_completeness:.1%}")
    print(f"failure-mode histogram:    {sm.failure_mode_histogram}")
    print(
        f"out-of-corpus hallucination: {am.n_hallucinated}/{am.n_probes} "
        f"({am.hallucination_rate:.1%})  by_ptype={am.by_ptype}"
    )
    print()

    r1 = json.loads((_BASELINE_DIR / "round1_sampled.json").read_text())
    r2 = json.loads((_BASELINE_DIR / "round2_adversarial.json").read_text())
    sampled_base = r1["frozen_sampled"].get("pass_rate")
    if sampled_base is not None:
        for d in compare_to_baseline({"pass_rate": sm.pass_rate}, {"pass_rate": sampled_base}):
            flag = "REGRESSED" if d.regressed else "ok"
            print(f"  pass_rate vs baseline: {d.current:.3f} (base {d.baseline:.3f}) [{flag}]")
    for d in compare_to_baseline(
        {"hallucination_rate": am.hallucination_rate},
        {"hallucination_rate": r2["headline"]["hallucination_rate"]},
    ):
        flag = "REGRESSED" if d.regressed else "ok"
        print(f"  hallucination vs baseline: {d.current:.3f} (base {d.baseline:.3f}) [{flag}]")


def main() -> int:
    if not os.environ.get(_FLAG):
        _print_dry()
        return 0
    missing = missing_env(LIVE_ENV_VARS)
    if missing:
        print(f"{_FLAG} is set but env vars are missing: {', '.join(missing)}", file=sys.stderr)
        print("source ./.env first (set -a && . ./.env && set +a)", file=sys.stderr)
        return 2

    print(f"running PAID eval ({_n_paid_calls()} calls, est ~${_estimated_cost():.2f})...")
    sampled = asyncio.run(_run_sampled())
    adversarial = asyncio.run(_run_adversarial())
    _print_metrics(sampled, adversarial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
