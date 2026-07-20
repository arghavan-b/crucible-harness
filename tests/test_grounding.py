"""Claim -> repo grounding tests (design §6.1)."""

from __future__ import annotations

from crucible.intake import (
    FakeClient,
    RepoBinding,
    gather_repo_signals,
    ground_claims,
)
from crucible.intake.extraction import ExtractedClaim, SourceRef
from crucible.planner import TemplatePlanner, analyze_repo
from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    Tolerance,
)


def _repo(root) -> None:
    (root / "train.py").write_text(
        "import argparse\n"
        "if __name__ == '__main__':\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--config')\n"
        "    p.add_argument('--seed')\n"
    )
    (root / "configs").mkdir()
    (root / "configs" / "method_x.yaml").write_text("lr: 0.1\n")
    (root / "configs" / "resnet50.yaml").write_text("lr: 0.1\n")
    (root / "Makefile").write_text("train:\n\tpython train.py\neval:\n\techo ok\n")
    (root / "README.md").write_text(
        "# Method X\n"
        "```bash\n"
        "python train.py --config configs/method_x.yaml\n"
        "python train.py --config configs/resnet50.yaml\n"
        "```\n"
    )


def _claim() -> ExtractedClaim:
    return ExtractedClaim(
        claim_id="c1", statement="Method X beats ResNet-50", metric="top1",
        method="method_x", baseline="resnet50", comparison="method_x > resnet50",
        reported_value=84.7, baseline_value=81.2, source=SourceRef(location="Table 2"),
    )


# --- signals ------------------------------------------------------------------


def test_gather_signals(tmp_path) -> None:
    _repo(tmp_path)
    s = gather_repo_signals(str(tmp_path))
    assert "configs/method_x.yaml" in s.config_files
    assert any("method_x" in c for c in s.readme_commands)
    assert "train" in s.makefile_targets
    assert "--config" in s.argparse_options["train.py"]


# --- heuristic grounding ------------------------------------------------------


def test_heuristic_grounding_matches_command_and_config(tmp_path) -> None:
    _repo(tmp_path)
    [binding] = ground_claims([_claim()], str(tmp_path))
    assert binding.run_command == "python train.py --config configs/method_x.yaml"
    assert binding.baseline_command == "python train.py --config configs/resnet50.yaml"
    assert "configs/method_x.yaml" in binding.config_files
    assert binding.confidence >= 0.5
    assert binding.entry_point == "train.py"


def test_heuristic_low_confidence_without_readme(tmp_path) -> None:
    (tmp_path / "train.py").write_text("if __name__ == '__main__':\n    pass\n")
    [b] = ground_claims([_claim()], str(tmp_path), default_entry="train.py")
    assert b.confidence < 0.5
    assert b.notes  # flagged for manual verification


# --- LLM grounding ------------------------------------------------------------


def test_llm_grounding(tmp_path) -> None:
    _repo(tmp_path)
    resp = {"bindings": [{
        "claim_id": "c1", "entry_point": "train.py",
        "run_command": "python train.py --config configs/method_x.yaml",
        "baseline_command": "python train.py --config configs/resnet50.yaml",
        "config_files": ["configs/method_x.yaml"], "confidence": 0.88,
        "sources": [{"file": "README.md", "detail": "usage"}],
    }]}
    [b] = ground_claims([_claim()], str(tmp_path), llm=FakeClient([resp]))
    assert isinstance(b, RepoBinding)
    assert b.confidence == 0.88
    assert b.sources[0].file == "README.md"


# --- planner uses bindings ----------------------------------------------------


def test_planner_uses_grounded_commands(tmp_path) -> None:
    _repo(tmp_path)
    analysis = analyze_repo(str(tmp_path))
    binding = ground_claims([_claim()], str(tmp_path))[0]
    spec = ExperimentSpec(
        experiment_id="e",
        hypothesis=Hypothesis(statement="h", type=HypothesisType.COMPARATIVE),
        source=Source(repo_uri="local://x", commit="c"),
        claims_under_test=[ClaimUnderTest(claim_id="c1", metric="top1",
            comparison="method_x > resnet50", reported_values={},
            tolerance=Tolerance(value=0.5))],
        positive_controls=[PositiveControl(control_id="pc1", description="d", metric="top1",
            expected=81.2, tolerance=Tolerance(value=0.01))],
    )
    plan = TemplatePlanner().plan(spec, analysis, bindings=[binding])
    full = next(s for s in plan.steps if s.step_id == "full_run")
    control = next(s for s in plan.steps if s.step_id == "positive_control_run")
    assert full.action.command == "python train.py --config configs/method_x.yaml"
    assert control.action.command == "python train.py --config configs/resnet50.yaml"
