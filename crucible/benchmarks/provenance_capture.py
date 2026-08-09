"""Capture one frozen controlled-task command without invoking the planner."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from crucible.certificate import (
    build_certificate,
    file_manifest,
    read_source,
    save_certificate,
    validate_replayable_source_snapshot,
)
from crucible.envmgr.manager import Environment, LocalEnvironmentManager
from crucible.executor.executor import RunResult, TransactionalExecutor
from crucible.runners.base import LinuxStraceRunner, Runner
from crucible.schemas import (
    Action,
    Evidence,
    ExecutionIntegrity,
    ExecutionPlan,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    Provenance,
    ReproducibilityCertificate,
    Source,
    Step,
    StepBudget,
    StepType,
    Verdict,
    VerdictStatus,
)
from crucible.trace.recorder import SQLiteTraceRecorder


@dataclass(frozen=True)
class FrozenCommandCapture:
    command: tuple[str, ...]
    submitted_command: str
    run: RunResult
    certificate: ReproducibilityCertificate


def _resolve_frozen_command(command: Sequence[str]) -> tuple[str, ...]:
    frozen = tuple(command)
    if not frozen or any(not token or "\x00" in token for token in frozen):
        raise ValueError("frozen command must contain non-empty, NUL-free argv tokens")
    return tuple(sys.executable if token == "{python}" else token for token in frozen)


def _placeholder_verdict(experiment_id: str, run: RunResult) -> Verdict:
    succeeded = run.all_succeeded
    reason = "provenance_adjudication_required"
    if not succeeded and run.step_results:
        reason = run.step_results[-1].failure_reason or "frozen_command_failed"
    return Verdict(
        experiment_id=experiment_id,
        claim_id="controlled_task",
        status=VerdictStatus.INCONCLUSIVE if succeeded else VerdictStatus.EXECUTION_FAILURE,
        confidence=0.0 if succeeded else 1.0,
        reason=reason,
        evidence=Evidence(execution_integrity=ExecutionIntegrity(all_steps_verified=succeeded)),
        provenance=Provenance(trace_id=run.trace_id),
    )


def capture_frozen_command(
    workspace: str | Path,
    command: Sequence[str],
    *,
    experiment_id: str,
    output_path: str | Path,
    timeout_s: int,
    container_digest: str,
    runner: Runner | None = None,
    network_policy: str | None = None,
) -> FrozenCommandCapture:
    """Run exactly one frozen argv under a monitored runner and save its certificate.

    The production caller uses ``LinuxStraceRunner`` inside the provenance
    container. Tests may inject another monitored runner while retaining the
    one-command plan and certificate-building path.
    """
    root = Path(workspace).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"workspace must be a regular directory: {root}")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    resolved = _resolve_frozen_command(command)
    submitted = shlex.join(resolved)
    source_files = read_source(str(root))
    source_checksums = file_manifest(str(root))
    validate_replayable_source_snapshot(source_files, source_checksums)

    spec = ExperimentSpec(
        experiment_id=experiment_id,
        hypothesis=Hypothesis(
            statement="Execute the frozen controlled-task procedure.",
            type=HypothesisType.REPRODUCTION,
        ),
        source=Source(repo_uri=f"controlled-task://{experiment_id}"),
    )
    plan = ExecutionPlan(
        experiment_id=experiment_id,
        steps=[
            Step(
                step_id="frozen_command",
                type=StepType.FULL_RUN,
                action=Action(kind="shell", command=submitted),
                verifier="exit_code_zero",
                budget=StepBudget(timeout_s=timeout_s, retries=0),
            )
        ],
    )

    effective_network_policy = network_policy or os.environ.get(
        "CRUCIBLE_NETWORK_POLICY", "unknown"
    )
    if effective_network_policy not in {"none", "unrestricted", "unknown"}:
        raise ValueError(f"unsupported network policy {effective_network_policy!r}")
    monitored_runner = runner or LinuxStraceRunner(
        network_policy=effective_network_policy  # type: ignore[arg-type]
    )

    with tempfile.TemporaryDirectory(prefix="crucible_frozen_capture_") as temporary:
        temp_root = Path(temporary)
        envmgr = LocalEnvironmentManager(base_dir=str(temp_root / "checkpoints"))
        recorder = SQLiteTraceRecorder(str(temp_root / "trace.sqlite"))
        env = Environment(env_id=f"env_{experiment_id}", working_dir=str(root))
        executor = TransactionalExecutor(
            envmgr=envmgr,
            runner=monitored_runner,
            recorder=recorder,
            env=env,
        )
        try:
            run = executor.execute(plan, validate=False)
        finally:
            recorder.close()

    verdict = _placeholder_verdict(experiment_id, run)
    certificate = build_certificate(
        spec=spec,
        plan=plan,
        run_result=run,
        working_dir=str(root),
        source_files=source_files,
        source_checksums=source_checksums,
        verdict=verdict,
        container_digest=container_digest,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    save_certificate(certificate, str(destination))
    return FrozenCommandCapture(
        command=resolved,
        submitted_command=submitted,
        run=run,
        certificate=certificate,
    )


__all__ = ["FrozenCommandCapture", "capture_frozen_command"]
