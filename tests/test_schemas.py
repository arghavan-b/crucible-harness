"""Smoke tests for the core schemas (design §4)."""

from __future__ import annotations

from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    StepType,
    Tolerance,
    Verdict,
    VerdictStatus,
)


def _sample_spec() -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_0042",
        hypothesis=Hypothesis(
            statement="Method X improves top-1 accuracy over baseline B on dataset D",
            type=HypothesisType.COMPARATIVE,
        ),
        source=Source(repo_uri="github.com/author/method-x", commit="a1b2c3d"),
        claims_under_test=[
            ClaimUnderTest(
                claim_id="c1",
                metric="top1_accuracy",
                comparison="method_x > baseline_b",
                reported_values={"method_x": 0.847, "baseline_b": 0.812},
                tolerance=Tolerance(value=0.005),
                seeds=[0, 1, 2],
            )
        ],
        positive_controls=[
            PositiveControl(
                control_id="pc1",
                description="Reproduce paper's reported baseline_b = 0.812",
                metric="top1_accuracy",
                expected=0.812,
                tolerance=Tolerance(value=0.01),
            )
        ],
    )


def test_spec_roundtrips_json() -> None:
    spec = _sample_spec()
    restored = ExperimentSpec.model_validate_json(spec.model_dump_json())
    assert restored == spec


def test_positive_control_present() -> None:
    assert _sample_spec().has_positive_control()


def test_ontology_has_eleven_step_types() -> None:
    assert len(list(StepType)) == 11


def test_verdict_defaults_inconclusive_reason() -> None:
    v = Verdict(
        experiment_id="exp_0042",
        claim_id="c1",
        status=VerdictStatus.INCONCLUSIVE,
        reason="control_failed",
    )
    assert v.status is VerdictStatus.INCONCLUSIVE
    assert v.reason == "control_failed"
