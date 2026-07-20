"""Planner — spec + repo analysis -> typed Execution Plan (design §4.2, §6.2).

A frontier LLM with three inputs: the spec, an automated repo analysis (file
tree, manifests, README, entry points, CI configs) and the step ontology +
schemas. Output is structured (JSON-schema enforced): the plan either parses and
validates or is regenerated. The planner NEVER executes and is not trusted —
the harness validates every plan (see crucible.validation).
"""

from __future__ import annotations

from crucible.schemas import ExecutionPlan, ExperimentSpec


class RepoAnalysis(dict):
    """File tree, manifests, README, entry points, CI configs."""


class Planner:
    def __init__(self, model: str = "frontier") -> None:
        self.model = model

    def analyze_repo(self, repo_path: str) -> RepoAnalysis:
        raise NotImplementedError("Stage 0, week 3.")

    def plan(self, spec: ExperimentSpec, analysis: RepoAnalysis) -> ExecutionPlan:
        """Emit a typed, ontology-conformant plan via structured output."""
        raise NotImplementedError("Stage 0, week 3.")
