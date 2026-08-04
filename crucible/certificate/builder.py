"""Assemble a Reproducibility Certificate from a completed run (design §4.4).

The certificate separates inputs from outputs:
  - source_files: the initial workspace (what replay re-seeds).
  - artifact_manifest: files produced by the run (what replay checks for
    byte-comparability).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from crucible.executor.executor import RunResult
from crucible.schemas import (
    ExecutionPlan,
    ExperimentSpec,
    NondeterminismPolicy,
    PinnedInputs,
    ReproducibilityCertificate,
    Verdict,
)

from .manifest import file_manifest, read_paths


def validate_replayable_source_snapshot(
    source_files: Mapping[str, str], source_checksums: Mapping[str, str]
) -> None:
    """Fail unless every pinned input can be recreated byte-for-byte as UTF-8 text."""
    checksum_paths = set(source_checksums)
    source_paths = set(source_files)
    if checksum_paths != source_paths:
        non_replayable = sorted(checksum_paths - source_paths)
        unpinned = sorted(source_paths - checksum_paths)
        details: list[str] = []
        if non_replayable:
            details.append(f"non-replayable inputs: {', '.join(non_replayable)}")
        if unpinned:
            details.append(f"unpinned source files: {', '.join(unpinned)}")
        suffix = f" ({'; '.join(details)})" if details else ""
        raise ValueError(
            "Stage-0 certificates require replayable text for every pinned input; "
            "source_checksums and source_files must name the same paths" + suffix
        )

    mismatches = [
        path
        for path, content in source_files.items()
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != source_checksums[path]
    ]
    if mismatches:
        raise ValueError(
            "source_checksums do not match the replayable UTF-8 source bytes for: "
            + ", ".join(sorted(mismatches))
        )


def build_certificate(
    spec: ExperimentSpec,
    plan: ExecutionPlan,
    run_result: RunResult,
    working_dir: str,
    source_files: dict[str, str],
    verdict: Verdict,
    source_checksums: dict[str, str],
    container_digest: str = "local://subprocess",
    policy: NondeterminismPolicy | None = None,
) -> ReproducibilityCertificate:
    """Build a self-contained certificate for the run just completed.

    `source_files` is captured BEFORE execution (the initial workspace). Anything
    present afterward that was not initial source is a produced artifact. `policy`
    declares which artifact divergences are acceptable on replay (default: empty
    = strict byte-equality). `source_checksums` must likewise be captured before
    execution; post-run files are never labeled as pinned inputs.
    """
    pinned_checksums = dict(source_checksums)
    validate_replayable_source_snapshot(source_files, pinned_checksums)
    final_manifest = file_manifest(working_dir)
    produced = {
        path: digest
        for path, digest in final_manifest.items()
        if pinned_checksums.get(path) != digest
    }
    pinned = PinnedInputs(
        repo_commit=spec.source.commit,
        dataset_checksums=pinned_checksums,
    )
    return ReproducibilityCertificate(
        experiment_id=spec.experiment_id,
        spec=spec,
        plan=plan,
        container_digest=container_digest,
        pinned_inputs=pinned,
        trace_id=run_result.trace_id,
        command_captures=run_result.command_captures,
        capture_summary=run_result.capture_summary,
        verdict=verdict,
        validation=run_result.validation,
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
