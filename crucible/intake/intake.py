"""Intake — repo (+ optional paper) -> Experiment Spec draft (design §4.1, §6.1).

For repo-only input, intake analyzes the project and drafts a spec with a
generated positive control — "no positive control, no verdict". Rich claim
extraction from a paper genuinely needs an LLM; that is exposed as an optional
seam (`llm`). The offline default produces a mechanically-checkable reproduction
spec: the entry point runs cleanly and emits an output artifact.
"""

from __future__ import annotations

import os
from typing import Protocol

from crucible.planner.analysis import RepoAnalysis, analyze_repo
from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    Tolerance,
)


class LLMClient(Protocol):
    def complete_json(self, prompt: str) -> dict: ...


def _experiment_id(repo_uri: str) -> str:
    slug = repo_uri.rstrip("/").split("/")[-1].replace(".git", "") or "repo"
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return f"exp_{slug}"


class Intake:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

    def prepare(
        self, repo_uri: str, root: str, paper_uri: str | None = None
    ) -> tuple[ExperimentSpec, RepoAnalysis]:
        """Analyze the repo and draft a spec. Returns both so the planner can
        reuse the analysis without re-walking the tree."""
        analysis = analyze_repo(root)
        spec = self._draft_spec(repo_uri, analysis, paper_uri)
        return spec, analysis

    def from_repo(self, repo_uri: str, root: str, paper_uri: str | None = None) -> ExperimentSpec:
        return self.prepare(repo_uri, root, paper_uri)[0]

    def generate_positive_control(self, analysis: RepoAnalysis) -> PositiveControl:
        # Mechanical control: the entry point runs and exits cleanly (exit 0).
        # A paper-backed intake would instead reproduce the paper's own baseline.
        return PositiveControl(
            control_id="pc1",
            description="entry point runs to completion and exits cleanly",
            metric="smoke_exit_code",
            expected=0.0,
            tolerance=Tolerance(value=0.0),
        )

    # --- drafting -------------------------------------------------------------

    def _draft_spec(
        self, repo_uri: str, analysis: RepoAnalysis, paper_uri: str | None
    ) -> ExperimentSpec:
        if self.llm is not None and paper_uri is not None:
            # Seam: an LLM extraction pass would fill claims/tolerances from the
            # paper here. Not exercised offline.
            pass

        claim = ClaimUnderTest(
            claim_id="c1",
            metric="output_artifact_produced",
            comparison="output_artifact_produced >= 1",
            reported_values={"output_artifact_produced": 1.0},
            tolerance=Tolerance(value=0.0),
            seeds=[0],
        )
        return ExperimentSpec(
            experiment_id=_experiment_id(repo_uri),
            hypothesis=Hypothesis(
                statement=f"The project at {os.path.basename(analysis.root)} runs and produces output.",
                type=HypothesisType.REPRODUCTION,
            ),
            source=Source(repo_uri=repo_uri, commit=None),
            claims_under_test=[claim],
            positive_controls=[self.generate_positive_control(analysis)],
        )
