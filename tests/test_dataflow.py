"""Predicate-grammar and dataflow validation tests (design §4.2)."""

from __future__ import annotations

from crucible.schemas import Action, ExecutionPlan, Step, StepType
from crucible.validation import Predicate, parse_predicate, validate_plan
from crucible.validation.gates import DEFAULT_INITIAL_FACTS


def _step(step_id, step_type, pre=None, post=None) -> Step:
    return Step(
        step_id=step_id,
        type=step_type,
        action=Action(kind="shell", command="true"),
        verifier="exit_code_zero",
        preconditions=pre or [],
        postconditions=post or [],
    )


# --- parser -------------------------------------------------------------------


def test_parse_predicate_forms() -> None:
    assert parse_predicate("gpu_available") == Predicate("gpu_available", ())
    assert parse_predicate('file_exists("a/b.txt")') == Predicate("file_exists", ("a/b.txt",))
    assert parse_predicate("version_satisfies(torch, >=2.0)") == Predicate(
        "version_satisfies", ("torch", ">=2.0")
    )


# --- predicate gate -----------------------------------------------------------


def test_unknown_predicate_flagged() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("s1", StepType.CONFIGURE, post=["frobnicate(x)"])],
    )
    assert any(v.gate == "unknown_predicate" for v in validate_plan(plan))


def test_bad_arity_flagged() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("s1", StepType.CONFIGURE, post=["file_exists()"])],
    )
    assert any(v.gate == "predicate_arity" for v in validate_plan(plan))


def test_malformed_predicate_flagged() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("s1", StepType.FULL_RUN, post=["exit_code == 0"])],
    )
    assert any(v.gate in {"predicate_syntax", "unknown_predicate"} for v in validate_plan(plan))


# --- dataflow gate ------------------------------------------------------------


def test_satisfied_dataflow_passes() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[
            _step("acquire", StepType.ACQUIRE_SOURCE, post=['file_exists("main.py")']),
            _step("run", StepType.FULL_RUN, pre=['file_exists("main.py")'],
                  post=['file_exists("out.json")']),
            _step("eval", StepType.EVALUATE_CLAIMS, pre=['file_exists("out.json")']),
        ],
    )
    assert [v for v in validate_plan(plan) if v.gate == "dataflow"] == []


def test_unsatisfied_precondition_flagged() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("run", StepType.FULL_RUN, pre=['file_exists("never_made.py")'])],
    )
    v = [x for x in validate_plan(plan) if x.gate == "dataflow"]
    assert v and "never established" in v[0].message


def test_ordering_violation_flagged() -> None:
    # Consumer appears before the producer -> ordering violation, not "never".
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[
            _step("eval", StepType.EVALUATE_CLAIMS, pre=['file_exists("out.json")']),
            _step("run", StepType.FULL_RUN, post=['file_exists("out.json")']),
        ],
    )
    v = [x for x in validate_plan(plan) if x.gate == "dataflow"]
    assert v and "later step" in v[0].message


def test_initial_facts_satisfy_preconditions() -> None:
    plan = ExecutionPlan(
        experiment_id="e",
        steps=[_step("run", StepType.FULL_RUN, pre=['file_exists("seed.py")'])],
    )
    facts = DEFAULT_INITIAL_FACTS | {Predicate("file_exists", ("seed.py",))}
    assert [v for v in validate_plan(plan, initial_facts=facts) if v.gate == "dataflow"] == []
