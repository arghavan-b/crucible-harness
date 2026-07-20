"""Severity levels, waivers, and validation provenance (design §4.2)."""

from __future__ import annotations

import pytest

from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.executor.executor import TransactionalExecutor
from crucible.runners.base import LocalSubprocessRunner
from crucible.schemas import (
    Action,
    Budget,
    ExecutionPlan,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    ScalePolicy,
    Severity,
    Source,
    Step,
    StepBudget,
    StepType,
    Waiver,
)
from crucible.trace.recorder import SQLiteTraceRecorder
from crucible.validation import PlanValidationError, validate


def _step(step_id, step_type, command="true", verifier="exit_code_zero", **kw) -> Step:
    return Step(step_id=step_id, type=step_type,
                action=Action(kind="shell", command=command), verifier=verifier, **kw)


def _spec(**kw) -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="e",
        hypothesis=Hypothesis(statement="h", type=HypothesisType.REPRODUCTION),
        source=Source(repo_uri="local://x", commit="c"),
        **kw,
    )


def test_warning_does_not_block() -> None:
    # A step timeout over budget is a WARNING, so the plan still passes.
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("run", StepType.FULL_RUN, budget=StepBudget(timeout_s=999_999))],
    )
    record = validate(plan, _spec(budget=Budget(max_wall_hours=1.0)))
    assert record.passed
    assert any(f.gate == "budget" and f.severity is Severity.WARNING for f in record.findings)


def test_error_blocks() -> None:
    plan = ExecutionPlan(experiment_id="e", steps=[_step("s", StepType.FULL_RUN, verifier="nope")])
    record = validate(plan)
    assert not record.passed
    assert record.blocking()


def test_waiver_downgrades_error() -> None:
    # Non-allowlisted egress is an ERROR; a waiver with justification clears it.
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("s", StepType.ACQUIRE_SOURCE, command="curl http://internal.corp/data -o d")],
    )
    assert not validate(plan).passed
    waived = validate(
        plan,
        waivers=[Waiver(gate="network_allowlist", reason="internal mirror, approved", author="arg")],
    )
    assert waived.passed
    f = next(f for f in waived.findings if f.gate == "network_allowlist")
    assert f.waived and f.waiver_reason == "internal mirror, approved"


def test_waiver_can_scope_to_step_and_substring() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[
            _step("a", StepType.ACQUIRE_SOURCE, command="curl http://one.corp/x -o x"),
            _step("b", StepType.ACQUIRE_DATA, command="curl http://two.corp/y -o y"),
        ],
    )
    # Waive only step 'a'; step 'b' still blocks.
    record = validate(plan, waivers=[Waiver(gate="network_allowlist", step_id="a", reason="ok")])
    assert not record.passed
    a = next(f for f in record.findings if f.step_id == "a")
    b = next(f for f in record.findings if f.step_id == "b")
    assert a.waived and not b.waived


def test_spec_waivers_used_by_executor(tmp_path) -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("s", StepType.ACQUIRE_SOURCE, command="curl http://internal.corp/d -o d && true",
                     verifier="exit_code_zero")],
    )
    spec = _spec(validation_waivers=[Waiver(gate="network_allowlist", reason="mirror")])
    ex = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(),
        runner=LocalSubprocessRunner(),
        recorder=SQLiteTraceRecorder(str(tmp_path / "t.sqlite")),
    )
    # Would raise without the waiver; with it, the run proceeds and is recorded.
    result = ex.execute(plan, spec=spec)
    assert result.validation is not None
    assert result.validation.passed
    assert any(f.waived for f in result.validation.findings)


def test_executor_still_blocks_unwaived_error(tmp_path) -> None:
    plan = ExecutionPlan(experiment_id="e", steps=[_step("s", StepType.FULL_RUN, verifier="nope")])
    ex = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(),
        runner=LocalSubprocessRunner(),
        recorder=SQLiteTraceRecorder(str(tmp_path / "t.sqlite")),
    )
    with pytest.raises(PlanValidationError):
        ex.execute(plan, spec=_spec())
