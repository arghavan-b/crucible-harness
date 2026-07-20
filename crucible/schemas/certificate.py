"""Reproducibility Certificate (design §4.4).

A signed bundle: spec + plan + container digest + pinned inputs (repo commit,
dataset checksums) + full trace reference + verdict. Anyone with the bundle can
`crucible replay` and get byte-comparable results (or a documented list of
nondeterminism sources). This is the artifact that makes Crucible's output
trustable by third parties.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .plan import ExecutionPlan
from .policy import NondeterminismPolicy
from .spec import ExperimentSpec
from .validation import ValidationRecord
from .verdict import Verdict


class PinnedInputs(BaseModel):
    repo_commit: str | None = None
    dataset_checksums: dict[str, str] = Field(default_factory=dict)


class ReproducibilityCertificate(BaseModel):
    experiment_id: str
    spec: ExperimentSpec
    plan: ExecutionPlan
    container_digest: str
    pinned_inputs: PinnedInputs = Field(default_factory=PinnedInputs)
    trace_id: str
    verdict: Verdict
    validation: ValidationRecord | None = Field(
        default=None, description="What the plan-validation gates checked, failed, and waived."
    )
    artifact_manifest: dict[str, str] = Field(
        default_factory=dict,
        description="Produced artifacts: relative path -> sha256. Replay checks byte-comparability against this.",
    )
    artifact_contents: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Produced-artifact contents (relative path -> text). Used by the nondeterminism policy "
            "for content-level comparison (numeric tolerance, normalization) when bytes differ. "
            "Stage-0 slice inlines text; production stores canonical forms in object storage."
        ),
    )
    nondeterminism_policy: NondeterminismPolicy = Field(
        default_factory=NondeterminismPolicy,
        description="Declares which artifact divergences are expected. Empty = strict byte-equality.",
    )
    source_files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Initial workspace inputs, relative path -> content. Stage-0 slice inlines source so a "
            "certificate is self-contained; production instead pins a git commit + dataset checksums "
            "(see pinned_inputs) and fetches them at replay time."
        ),
    )
    signature: str | None = Field(default=None, description="Filled by the certificate signer.")
