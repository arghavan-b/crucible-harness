"""Intake — repo/paper -> Experiment Spec draft (design §4.1, §6.1).

Input forms: (a) full spec, (b) paper PDF + repo URL, (c) repo URL alone.
For (b)/(c) an LLM extraction pass drafts the spec: claims table, entry points,
and — critically — a generated positive control. No positive control, no verdict.
"""

from __future__ import annotations

from crucible.schemas import ExperimentSpec, PositiveControl


class Intake:
    def from_repo(self, repo_uri: str, paper_uri: str | None = None) -> ExperimentSpec:
        """Analyze a repo (+optional paper) and draft an ExperimentSpec.

        Must pin the commit and guarantee at least one positive control.
        """
        raise NotImplementedError("Stage 0, week 3.")

    def generate_positive_control(self, spec: ExperimentSpec) -> PositiveControl:
        """Usually: reproduce the paper's own reported baseline number."""
        raise NotImplementedError("Stage 0, week 3.")
