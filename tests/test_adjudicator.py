"""Verdict Adjudicator tests (design §8)."""

from __future__ import annotations

from crucible.adjudicator import Observations, adjudicate
from crucible.executor.executor import RunResult, StepResult
from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    StepState,
    Tolerance,
    VerdictStatus,
)


def _spec(htype=HypothesisType.REPRODUCTION, comparison="prediction_count > 0",
          reported=None, tol=0.0, with_control=True) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="e",
        hypothesis=Hypothesis(statement="h", type=htype),
        source=Source(repo_uri="local://x", commit="c"),
        claims_under_test=[
            ClaimUnderTest(
                claim_id="c1",
                metric="prediction_count",
                comparison=comparison,
                reported_values=reported if reported is not None else {"prediction_count": 2.0},
                tolerance=Tolerance(value=tol),
                seeds=[0, 1, 2],
            )
        ],
        positive_controls=(
            [PositiveControl(control_id="pc1", description="d", metric="m", expected=2.0,
                             tolerance=Tolerance(value=0.0))]
            if with_control else []
        ),
    )


def _ok_run() -> RunResult:
    return RunResult(
        experiment_id="e", trace_id="trace_x",
        step_results=[StepResult(step_id="s1", state=StepState.SUCCEEDED)],
    )


def _failed_run() -> RunResult:
    return RunResult(
        experiment_id="e", trace_id="trace_x", stopped_at="s2",
        step_results=[
            StepResult(step_id="s1", state=StepState.SUCCEEDED),
            StepResult(step_id="s2", state=StepState.FAILED, failure_reason="verifier 'x' failed"),
        ],
    )


# --- gating steps -------------------------------------------------------------


def test_no_positive_control_is_inconclusive() -> None:
    v = adjudicate(_spec(with_control=False), _ok_run(), "c1",
                   Observations(claim_series={"prediction_count": [2.0]}))
    assert v.status is VerdictStatus.INCONCLUSIVE
    assert v.reason == "no_positive_control"


def test_failed_control_is_inconclusive() -> None:
    v = adjudicate(_spec(), _ok_run(), "c1",
                   Observations(control_values={"pc1": 5.0}))  # expected 2.0, tol 0
    assert v.status is VerdictStatus.INCONCLUSIVE
    assert v.reason == "control_failed"
    assert v.evidence.positive_control.status == "failed"


def test_execution_failure_reports_deepest_cause() -> None:
    v = adjudicate(_spec(), _failed_run(), "c1",
                   Observations(control_values={"pc1": 2.0},
                                claim_series={"prediction_count": [2.0]}))
    assert v.status is VerdictStatus.EXECUTION_FAILURE
    assert "verifier 'x' failed" in (v.reason or "")


# --- reproduction claims ------------------------------------------------------


def test_reproduction_success() -> None:
    v = adjudicate(_spec(tol=0.0), _ok_run(), "c1",
                   Observations(control_values={"pc1": 2.0},
                                claim_series={"prediction_count": [2.0]}))
    assert v.status is VerdictStatus.SUCCESS
    assert "reproduced" in v.evidence.result.conclusion


def test_reproduction_miss_is_result_negative() -> None:
    v = adjudicate(_spec(tol=0.0, reported={"prediction_count": 2.0}), _ok_run(), "c1",
                   Observations(control_values={"pc1": 2.0},
                                claim_series={"prediction_count": [3.0]}))
    assert v.status is VerdictStatus.RESULT_NEGATIVE


# --- comparative claims -------------------------------------------------------


def _comp_spec():
    return _spec(htype=HypothesisType.COMPARATIVE, comparison="method_x > baseline_b",
                 reported={})


def test_comparative_significant_is_success() -> None:
    obs = Observations(
        control_values={"pc1": 2.0},
        claim_series={"method_x": [0.85, 0.86, 0.87], "baseline_b": [0.80, 0.79, 0.81]},
    )
    v = adjudicate(_comp_spec(), _ok_run(), "c1", obs)
    assert v.status is VerdictStatus.SUCCESS
    assert "p=" in v.evidence.result.test


def test_comparative_not_significant_is_result_negative() -> None:
    obs = Observations(
        control_values={"pc1": 2.0},
        claim_series={"method_x": [0.81, 0.80, 0.82], "baseline_b": [0.80, 0.81, 0.79]},
    )
    v = adjudicate(_comp_spec(), _ok_run(), "c1", obs)
    assert v.status is VerdictStatus.RESULT_NEGATIVE
    assert "not distinguishable" in v.evidence.result.conclusion


def test_comparative_insufficient_seeds_is_inconclusive() -> None:
    obs = Observations(
        control_values={"pc1": 2.0},
        claim_series={"method_x": [0.85], "baseline_b": [0.80]},
    )
    v = adjudicate(_comp_spec(), _ok_run(), "c1", obs)
    assert v.status is VerdictStatus.INCONCLUSIVE
    assert v.reason == "insufficient_seeds_for_significance"
