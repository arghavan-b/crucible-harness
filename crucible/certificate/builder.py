"""Assemble a Reproducibility Certificate from a completed run (design §4.4).

The certificate separates inputs from outputs:
  - source_files: the initial workspace (what replay re-seeds).
  - artifact_manifest: files produced by the run (what replay checks for
    byte-comparability).
"""

from __future__ import annotations

import json

from crucible.executor.executor import RunResult
from crucible.schemas import (
    ExecutionPlan,
    ExperimentSpec,
    NondeterminismPolicy,
    PinnedInputs,
    ReproducibilityCertificate,
    Verdict,
)

from .manifest import file_manifest, read_paths, read_source


def build_certificate(
    spec: ExperimentSpec,
    plan: ExecutionPlan,
    run_result: RunResult,
    working_dir: str,
    source_files: dict[str, str],
    verdict: Verdict,
    container_digest: str = "local://subprocess",
    policy: NondeterminismPolicy | None = None,
) -> ReproducibilityCertificate:
    """Build a self-contained certificate for the run just completed.

    `source_files` is captured BEFORE execution (the initial workspace). Anything
    present afterward that was not initial source is a produced artifact. `policy`
    declares which artifact divergences are acceptable on replay (default: empty
    = strict byte-equality).
    """
    produced = file_manifest(working_dir, exclude=frozenset(source_files))
    pinned = PinnedInputs(
        repo_commit=spec.source.commit,
        dataset_checksums={p: h for p, h in file_manifest(working_dir).items() if p in source_files},
    )
    return ReproducibilityCertificate(
        experiment_id=spec.experiment_id,
        spec=spec,
        plan=plan,
        container_digest=container_digest,
        pinned_inputs=pinned,
        trace_id=run_result.trace_id,
        verdict=verdict,
        artifact_manifest=produced,
        artifact_contents=read_paths(working_dir, frozenset(produced)),
        nondeterminism_policy=policy or NondeterminismPolicy(),
        source_files=source_files,
    )


def save_certificate(cert: ReproducibilityCertificate, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(cert.model_dump_json(indent=2))


def load_certificate(path: str) -> ReproducibilityCertificate:
    with open(path, encoding="utf-8") as f:
        return ReproducibilityCertificate.model_validate(json.load(f))
