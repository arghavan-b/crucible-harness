"""Verifier arg-schema validation tests (design §4.2, §7)."""

from __future__ import annotations

from crucible.schemas import Action, ExecutionPlan, Step, StepType
from crucible.validation import validate_plan
from crucible.verifiers import catalog


def _step(verifier, verifier_args=None) -> Step:
    return Step(
        step_id="s1",
        type=StepType.EVALUATE_CLAIMS,
        action=Action(kind="shell", command="true"),
        verifier=verifier,
        verifier_args=verifier_args or {},
        preconditions=[],
        postconditions=[],
    )


def _plan(step) -> ExecutionPlan:
    return ExecutionPlan(experiment_id="e", steps=[step])


# --- catalog helpers ----------------------------------------------------------


def test_validate_args_direct() -> None:
    assert catalog.validate_args("file_exists", {"path": "x", "min_size": 4}) == []
    assert catalog.validate_args("file_exists", {}) == ["missing required arg 'path'"]
    assert catalog.is_implemented("exit_code_zero")
    assert not catalog.is_implemented("checksum_matches")


# --- gate ---------------------------------------------------------------------


def test_valid_args_pass() -> None:
    plan = _plan(_step("file_exists", {"path": "out.json", "min_size": 2}))
    assert not [v for v in validate_plan(plan) if v.gate in {"verifier_args", "verifier_not_implemented"}]


def test_missing_required_arg() -> None:
    plan = _plan(_step("file_exists", {}))
    v = [x for x in validate_plan(plan) if x.gate == "verifier_args"]
    assert v and "missing required arg 'path'" in v[0].message


def test_wrong_type_arg() -> None:
    plan = _plan(_step("file_exists", {"path": "out.json", "min_size": "big"}))
    v = [x for x in validate_plan(plan) if x.gate == "verifier_args"]
    assert v and "must be int" in v[0].message


def test_bool_is_not_int() -> None:
    plan = _plan(_step("file_exists", {"path": "o", "min_size": True}))
    assert any(x.gate == "verifier_args" and "bool" in x.message for x in validate_plan(plan))


def test_unknown_arg() -> None:
    # An unknown/extra arg is a WARNING (ignored at runtime), not a blocking error.
    plan = _plan(_step("exit_code_zero", {"surprise": 1}))
    v = [x for x in validate_plan(plan) if x.gate == "verifier_args_unknown"]
    assert v and "unknown arg 'surprise'" in v[0].message


def test_missing_packages_list() -> None:
    plan = _plan(_step("imports_resolvable", {"python": "python3"}))
    assert any("missing required arg 'packages'" in x.message for x in validate_plan(plan))


def test_catalogued_but_unimplemented_verifier() -> None:
    plan = _plan(_step("checksum_matches", {"path": "a", "expected": "deadbeef"}))
    assert any(v.gate == "verifier_not_implemented" for v in validate_plan(plan))
