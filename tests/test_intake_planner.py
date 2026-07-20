"""Intake + planner tests (design §6.1, §6.2, §4.2)."""

from __future__ import annotations

import pytest

from crucible.intake import Intake
from crucible.planner import (
    LLMPlanner,
    PlannerError,
    TemplatePlanner,
    analyze_repo,
)
from crucible.validation import validate


def _make_repo(root, with_manifest=True, with_entry=True) -> None:
    if with_entry:
        (root / "inference.py").write_text(
            "import json\n"
            "if __name__ == '__main__':\n"
            "    json.dump([1, 2], open('outputs/result.json', 'w'))\n"
        )
    if with_manifest:
        (root / "requirements.txt").write_text("numpy>=1.20\nscikit-learn==1.3.0  # comment\n")
    (root / "README.md").write_text("# Demo\nRuns on GPU with torch.\n")


# --- repo analysis ------------------------------------------------------------


def test_analyze_repo(tmp_path) -> None:
    _make_repo(tmp_path)
    a = analyze_repo(str(tmp_path))
    assert a.language == "python"
    assert "requirements.txt" in a.dependency_manifests
    assert "inference.py" in a.entry_points
    assert a.top_level_packages == ["numpy", "scikit-learn"]
    assert a.cuda_required  # README mentions torch/GPU


def test_analyze_repo_entrypoint_via_main_guard(tmp_path) -> None:
    (tmp_path / "weird_name.py").write_text("if __name__ == '__main__':\n    print('hi')\n")
    a = analyze_repo(str(tmp_path))
    assert "weird_name.py" in a.entry_points


# --- intake -------------------------------------------------------------------


def test_intake_drafts_valid_spec(tmp_path) -> None:
    _make_repo(tmp_path)
    spec, analysis = Intake().prepare("local://demo-repo", root=str(tmp_path))
    assert spec.experiment_id == "exp_demo-repo"
    assert spec.has_positive_control()
    assert spec.claims_under_test


# --- template planner ---------------------------------------------------------


def test_template_plan_is_valid(tmp_path) -> None:
    _make_repo(tmp_path)
    spec, analysis = Intake().prepare("local://demo-repo", root=str(tmp_path))
    plan = TemplatePlanner().plan(spec, analysis)
    record = validate(plan, spec)
    assert record.passed, record.summary()
    # It has the required ordering-sensitive steps.
    types = [s.type.value for s in plan.steps]
    assert types.index("smoke_run") < types.index("full_run")
    assert types.index("positive_control_run") < types.index("evaluate_claims")


def test_template_plan_without_manifest_is_valid(tmp_path) -> None:
    _make_repo(tmp_path, with_manifest=False)
    spec, analysis = Intake().prepare("local://x", root=str(tmp_path))
    plan = TemplatePlanner().plan(spec, analysis)
    assert validate(plan, spec).passed
    assert not any(s.step_id == "provision_dependencies" for s in plan.steps)


def test_planner_errors_without_entrypoint(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("no code here")
    spec, analysis = Intake().prepare("local://x", root=str(tmp_path))
    with pytest.raises(PlannerError):
        TemplatePlanner().plan(spec, analysis)


# --- LLM planner with regenerate loop -----------------------------------------


class _FakeClient:
    """Returns queued plan dicts in order (simulating LLM structured output)."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)

    def complete_json(self, prompt: str) -> dict:
        return self._responses.pop(0)


def test_llm_planner_returns_valid_plan(tmp_path) -> None:
    _make_repo(tmp_path)
    spec, analysis = Intake().prepare("local://x", root=str(tmp_path))
    good = TemplatePlanner().plan(spec, analysis).model_dump()
    plan = LLMPlanner(_FakeClient([good])).plan(spec, analysis)
    assert validate(plan, spec).passed


def test_llm_planner_regenerates_after_bad_plan(tmp_path) -> None:
    _make_repo(tmp_path)
    spec, analysis = Intake().prepare("local://x", root=str(tmp_path))
    good = TemplatePlanner().plan(spec, analysis).model_dump()
    bad = {"experiment_id": spec.experiment_id, "steps": [
        {"step_id": "s1", "type": "full_run",
         "action": {"kind": "shell", "command": "true"}, "verifier": "nope"}
    ]}
    # First response invalid (unknown verifier), second valid -> loop recovers.
    plan = LLMPlanner(_FakeClient([bad, good])).plan(spec, analysis)
    assert validate(plan, spec).passed


def test_llm_planner_raises_when_never_valid(tmp_path) -> None:
    _make_repo(tmp_path)
    spec, analysis = Intake().prepare("local://x", root=str(tmp_path))
    bad = {"experiment_id": spec.experiment_id, "steps": [
        {"step_id": "s1", "type": "full_run",
         "action": {"kind": "shell", "command": "true"}, "verifier": "nope"}
    ]}
    with pytest.raises(PlannerError):
        LLMPlanner(_FakeClient([bad, bad, bad])).plan(spec, analysis)
