"""Nondeterminism policy: classification unit tests + replay integration."""

from __future__ import annotations

from crucible.certificate import (
    build_certificate,
    classify_divergence,
    default_policy,
    replay_certificate,
)
from crucible.certificate.manifest import read_source
from crucible.certificate.policy import Classification
from crucible.schemas import (
    ArtifactRule,
    NondeterminismPolicy,
    RuleMode,
    ToleranceType,
    Verdict,
    VerdictStatus,
)
from examples.demo_local import build_demo_spec, build_executor


# --- unit tests on the classifier ---------------------------------------------


def test_exempt_rule_is_expected() -> None:
    policy = NondeterminismPolicy(rules=[ArtifactRule(pattern="*.log", mode=RuleMode.EXEMPT)])
    j = classify_divergence(policy, "run.log", "monday", "tuesday")
    assert j.classification is Classification.EXPECTED


def test_numeric_json_within_tolerance_is_expected() -> None:
    policy = NondeterminismPolicy(
        rules=[ArtifactRule(pattern="*.json", mode=RuleMode.NUMERIC_JSON, tolerance=0.01)]
    )
    j = classify_divergence(policy, "metrics.json", '{"acc": 0.812}', '{"acc": 0.815}')
    assert j.classification is Classification.EXPECTED


def test_numeric_json_beyond_tolerance_is_unexpected() -> None:
    policy = NondeterminismPolicy(
        rules=[ArtifactRule(pattern="*.json", mode=RuleMode.NUMERIC_JSON, tolerance=0.01)]
    )
    j = classify_divergence(policy, "metrics.json", '{"acc": 0.812}', '{"acc": 0.900}')
    assert j.classification is Classification.UNEXPECTED
    assert "tolerance" in (j.detail or "")


def test_relative_tolerance() -> None:
    policy = NondeterminismPolicy(
        rules=[
            ArtifactRule(
                pattern="*.json",
                mode=RuleMode.NUMERIC_JSON,
                tolerance=0.05,
                tolerance_type=ToleranceType.RELATIVE,
            )
        ]
    )
    j = classify_divergence(policy, "m.json", '{"x": 100.0}', '{"x": 103.0}')  # 3% < 5%
    assert j.classification is Classification.EXPECTED


def test_normalize_strips_volatile_pattern() -> None:
    policy = NondeterminismPolicy(
        rules=[
            ArtifactRule(
                pattern="*.txt",
                mode=RuleMode.NORMALIZE,
                strip_pattern=r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            )
        ]
    )
    a = "run at 2026-01-01T10:00:00 done"
    b = "run at 2026-07-16T22:31:09 done"
    assert classify_divergence(policy, "o.txt", a, b).classification is Classification.EXPECTED


def test_no_rule_means_unexpected() -> None:
    j = classify_divergence(NondeterminismPolicy(), "x.bin", "a", "b")
    assert j.classification is Classification.UNEXPECTED


# --- integration: policy makes an otherwise-failing replay reproduce -----------


def test_exempted_log_still_reproduces(tmp_path) -> None:
    def inject(plan):
        # Emit a nondeterministic log alongside the deterministic artifact.
        plan.steps[1].action.command = (
            "python3 inference.py && python3 -c "
            "\"import random; open('run.log','w').write(str(random.random()))\""
        )

    executor, plan = build_executor(db_path=str(tmp_path / "orig.sqlite"))
    inject(plan)
    source = read_source(executor._env.working_dir)
    result = executor.execute(plan)
    cert = build_certificate(
        spec=build_demo_spec(),
        plan=plan,
        run_result=result,
        working_dir=executor._env.working_dir,
        source_files=source,
        verdict=Verdict(experiment_id=plan.experiment_id, claim_id="c1", status=VerdictStatus.SUCCESS),
        policy=default_policy(),  # exempts *.log
    )

    report = replay_certificate(cert)
    assert report.reproduced  # log divergence tolerated
    assert "predictions.json" in report.matched
    assert "run.log" in [j.path for j in report.expected_divergence]
