"""Offline, $0 unit tests for the eval metric aggregation (issue #34).

NO network, NO API keys, NO DB. Feeds hand-built graded-record sets through
``tests.eval.metrics`` and asserts the headline metrics, the failure-mode
histogram, the adversarial hallucination rate, and the baseline-delta logic.
Also exercises the recorded baselines and the frozen probe corpus so a drift in
either is caught offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.metrics import (
    FAIL_MODES,
    GradedRecord,
    ProbeRecord,
    aggregate,
    aggregate_adversarial,
    compare_to_baseline,
    metrics_as_dict,
)
from tests.eval.probes_out_of_corpus import OUT_OF_CORPUS_PROBES

_BASELINE_DIR = Path(__file__).resolve().parents[1] / "eval" / "baselines"


def test_graded_record_rejects_unknown_failure_mode() -> None:
    with pytest.raises(ValueError, match="unknown failure_mode"):
        GradedRecord(case_id="x", passed=False, failure_mode="not_a_real_mode")


def test_aggregate_pass_rate_and_histogram() -> None:
    records = [
        GradedRecord(case_id="a", passed=True),
        GradedRecord(case_id="b", passed=True),
        GradedRecord(case_id="c", passed=False, failure_mode="paraphrase_recall_miss"),
        GradedRecord(case_id="d", passed=False, failure_mode="wrong_value"),
        GradedRecord(case_id="e", passed=False, failure_mode="wrong_value"),
    ]
    m = aggregate(records)
    assert m.n == 5
    assert m.n_pass == 2
    assert m.pass_rate == pytest.approx(0.4)
    assert m.failure_mode_histogram == {"paraphrase_recall_miss": 1, "wrong_value": 2}
    # Contamination = wrong-doc/wrong_value share.
    assert m.contamination_pct == pytest.approx(40.0)


def test_aggregate_recall_and_tool_size_completeness() -> None:
    records = [
        GradedRecord(
            case_id="a",
            passed=True,
            retrieved_expected=True,
            tool_size_expected=True,
            tool_size_present=True,
        ),
        GradedRecord(
            case_id="b",
            passed=False,
            failure_mode="missing_procedure_caveat",
            retrieved_expected=True,
            tool_size_expected=True,
            tool_size_present=False,
        ),
        GradedRecord(
            case_id="c",
            passed=False,
            failure_mode="paraphrase_recall_miss",
            retrieved_expected=False,
        ),
    ]
    m = aggregate(records)
    assert m.recall_at_k == pytest.approx(2 / 3)
    # Two cases expected a tool size, one supplied it.
    assert m.tool_size_completeness == pytest.approx(0.5)


def test_aggregate_empty_is_zero_not_crash() -> None:
    m = aggregate([])
    assert m.n == 0
    assert m.pass_rate == 0.0
    assert m.recall_at_k == 0.0


def test_aggregate_adversarial_hallucination_rate_and_split() -> None:
    records = [
        ProbeRecord(probe_id="A00", ptype="other_brand", hallucinated=False),
        ProbeRecord(probe_id="A01", ptype="other_brand", hallucinated=True),
        ProbeRecord(probe_id="A02", ptype="fabricated", hallucinated=False),
        ProbeRecord(probe_id="A03", ptype="wrong_premise", hallucinated=True),
    ]
    m = aggregate_adversarial(records)
    assert m.n_probes == 4
    assert m.n_hallucinated == 2
    assert m.hallucination_rate == pytest.approx(0.5)
    assert m.by_ptype["other_brand"] == {"probes": 2, "hallucinated": 1}
    assert m.by_ptype["wrong_premise"] == {"probes": 1, "hallucinated": 1}


def test_compare_to_baseline_flags_regression_and_improvement() -> None:
    baseline = {"pass_rate": 0.56, "hallucination_rate": 0.15}
    current = {"pass_rate": 0.50, "hallucination_rate": 0.10}
    deltas = {d.metric: d for d in compare_to_baseline(current, baseline)}

    # pass_rate dropped -> regression (higher is better).
    assert deltas["pass_rate"].regressed
    assert deltas["pass_rate"].delta == pytest.approx(-0.06)
    # hallucination_rate dropped -> improvement (lower is better), not a regression.
    assert not deltas["hallucination_rate"].regressed
    assert deltas["hallucination_rate"].delta == pytest.approx(-0.05)


def test_compare_to_baseline_tolerance_absorbs_noise() -> None:
    baseline = {"pass_rate": 0.56}
    current = {"pass_rate": 0.54}
    # Without tolerance this regresses; with a 5-point band it does not.
    assert compare_to_baseline(current, baseline)[0].regressed
    assert not compare_to_baseline(current, baseline, tolerance=0.05)[0].regressed


def test_metrics_as_dict_drops_histogram() -> None:
    m = aggregate([GradedRecord(case_id="a", passed=True)])
    d = metrics_as_dict(m)
    assert "failure_mode_histogram" not in d
    assert d["pass_rate"] == 1.0


def test_fail_modes_are_unique() -> None:
    assert len(FAIL_MODES) == len(set(FAIL_MODES))


def test_out_of_corpus_probe_corpus_is_frozen_at_39() -> None:
    assert len(OUT_OF_CORPUS_PROBES) == 39
    ptypes = {p.ptype for p in OUT_OF_CORPUS_PROBES}
    assert ptypes == {"other_brand", "fabricated", "wrong_premise", "edge"}
    # ids are unique and stable
    assert len({p.probe_id for p in OUT_OF_CORPUS_PROBES}) == 39
    assert all(p.expected_behavior == "abstain" for p in OUT_OF_CORPUS_PROBES)


def test_recorded_baselines_load_and_match_issue_headline() -> None:
    round1 = json.loads((_BASELINE_DIR / "round1_sampled.json").read_text())
    round2 = json.loads((_BASELINE_DIR / "round2_adversarial.json").read_text())

    # Round-1 source-grounded headline: 68/121 ≈ 56 %.
    assert round1["full_run"]["n_pass"] == 68
    assert round1["full_run"]["n"] == 121
    assert round1["full_run"]["pass_rate"] == pytest.approx(68 / 121, abs=1e-3)

    # Round-2 adversarial headline: 6/39 ≈ 15 % hallucination.
    assert round2["headline"]["n_probes"] == 39
    assert round2["headline"]["n_hallucinated"] == 6
    assert round2["headline"]["hallucination_rate"] == pytest.approx(6 / 39, abs=1e-3)
