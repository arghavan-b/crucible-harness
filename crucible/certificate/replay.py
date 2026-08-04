"""Replay a Reproducibility Certificate (design §4.4, §6.5).

Re-seed the pinned source into a fresh environment, re-run the exact plan, and
compare produced artifacts to the certificate's manifest. Divergences are then
classified by the certificate's nondeterminism policy into EXPECTED (tolerable)
and UNEXPECTED (a real reproduction failure). Only UNEXPECTED divergence — plus
missing artifacts or execution failure — breaks reproduction.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from crucible.envmgr.manager import EnvironmentManager, LocalEnvironmentManager
from crucible.executor.executor import TransactionalExecutor
from crucible.runners.base import LocalSubprocessRunner, Runner
from crucible.schemas import NondeterminismPolicy, ReproducibilityCertificate
from crucible.trace.recorder import SQLiteTraceRecorder, TraceRecorder

from .manifest import file_manifest, read_paths
from .policy import (
    ArtifactJudgement,
    Classification,
    classify_divergence,
    classify_unexpected_artifact,
)


@dataclass
class ReplayReport:
    experiment_id: str
    original_trace_id: str
    replay_trace_id: str
    execution_succeeded: bool
    matched: list[str] = field(default_factory=list)  # byte-identical
    expected_divergence: list[ArtifactJudgement] = field(default_factory=list)
    unexpected_divergence: list[ArtifactJudgement] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # certified but not produced
    unexpected_artifacts: list[ArtifactJudgement] = field(default_factory=list)

    @property
    def reproduced(self) -> bool:
        return (
            self.execution_succeeded
            and not self.unexpected_divergence
            and not self.missing
            and not self.unexpected_artifacts
        )

    def summary(self) -> str:
        if self.reproduced:
            extra = (
                f" ({len(self.expected_divergence)} expected divergence)"
                if self.expected_divergence
                else ""
            )
            return f"REPRODUCED — {len(self.matched)} artifact(s) byte-identical{extra}."
        reasons: list[str] = []
        if not self.execution_succeeded:
            reasons.append("execution did not complete")
        if self.unexpected_divergence:
            reasons.append(
                "nondeterministic artifacts: "
                + ", ".join(j.path for j in self.unexpected_divergence)
            )
        if self.missing:
            reasons.append(f"missing artifacts: {', '.join(self.missing)}")
        if self.unexpected_artifacts:
            reasons.append(
                "unexpected artifacts: " + ", ".join(j.path for j in self.unexpected_artifacts)
            )
        return "NOT REPRODUCED — " + "; ".join(reasons)


def replay_certificate(
    cert: ReproducibilityCertificate,
    policy: NondeterminismPolicy | None = None,
    envmgr: EnvironmentManager | None = None,
    runner: Runner | None = None,
    recorder: TraceRecorder | None = None,
) -> ReplayReport:
    policy = policy or cert.nondeterminism_policy
    envmgr = envmgr or LocalEnvironmentManager()
    runner = runner or LocalSubprocessRunner()
    recorder = recorder or SQLiteTraceRecorder(
        os.path.join(tempfile.mkdtemp(prefix="crucible_replay_"), "replay.sqlite")
    )

    env = envmgr.provision()
    for rel, content in cert.source_files.items():
        dest = os.path.join(env.working_dir, rel)
        os.makedirs(os.path.dirname(dest) or env.working_dir, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)

    executor = TransactionalExecutor(envmgr=envmgr, runner=runner, recorder=recorder, env=env)
    run = executor.execute(cert.plan)

    final_manifest = file_manifest(env.working_dir)
    initial_checksums = cert.pinned_inputs.dataset_checksums
    produced = (
        {
            path: digest
            for path, digest in final_manifest.items()
            if initial_checksums.get(path) != digest
        }
        if initial_checksums
        else {
            path: digest for path, digest in final_manifest.items() if path not in cert.source_files
        }
    )
    expected = cert.artifact_manifest
    replay_contents = read_paths(env.working_dir, frozenset(produced))

    matched: list[str] = []
    expected_div: list[ArtifactJudgement] = []
    unexpected_div: list[ArtifactJudgement] = []
    missing: list[str] = [
        f"pinned_input:{path}" for path in sorted(set(initial_checksums) - set(cert.source_files))
    ]

    for path, digest in expected.items():
        if path not in produced:
            missing.append(path)
        elif produced[path] == digest:
            matched.append(path)
        else:
            judgement = classify_divergence(
                policy, path, cert.artifact_contents.get(path), replay_contents.get(path)
            )
            if judgement.classification is Classification.EXPECTED:
                expected_div.append(judgement)
            else:
                unexpected_div.append(judgement)

    unexpected_artifacts: list[ArtifactJudgement] = []
    for path in sorted(set(produced) - set(expected)):
        judgement = classify_unexpected_artifact(policy, path)
        if judgement.classification is Classification.EXPECTED:
            expected_div.append(judgement)
        else:
            unexpected_artifacts.append(judgement)

    return ReplayReport(
        experiment_id=cert.experiment_id,
        original_trace_id=cert.trace_id,
        replay_trace_id=run.trace_id,
        execution_succeeded=run.all_succeeded,
        matched=sorted(matched),
        expected_divergence=expected_div,
        unexpected_divergence=unexpected_div,
        missing=sorted(missing),
        unexpected_artifacts=unexpected_artifacts,
    )
