"""Experiment-grade orchestration for a controlled provenance suite."""

from __future__ import annotations

import platform
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .provenance import ControlledSuite, PilotTaskError
from .provenance_container import (
    LinuxContainerExecution,
    ensure_linux_provenance_image,
    run_frozen_strategy_in_container,
)
from .provenance_ledger import (
    AppendOnlyExperimentLedger,
    ArtifactIntegrity,
    ControlledCase,
    ControlledRunManifest,
    artifact_integrity,
    sha256_path,
    utc_now,
    verify_experiment_ledger,
    write_run_manifest,
)

ImageResolver = Callable[..., str]
StrategyExecutor = Callable[..., LinuxContainerExecution]


@dataclass(frozen=True)
class ControlledCaseAttempt:
    case: ControlledCase
    execution: LinuxContainerExecution | None
    artifacts: tuple[ArtifactIntegrity, ...]
    error_type: str | None = None
    error_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.execution is not None


@dataclass(frozen=True)
class ControlledExperimentResult:
    manifest: ControlledRunManifest
    manifest_path: Path
    ledger_path: Path
    container_digest: str | None
    attempts: tuple[ControlledCaseAttempt, ...]
    suite_error_type: str | None = None
    suite_error_message: str | None = None

    @property
    def failures(self) -> tuple[ControlledCaseAttempt, ...]:
        return tuple(attempt for attempt in self.attempts if not attempt.succeeded)

    @property
    def mismatches(self) -> tuple[ControlledCaseAttempt, ...]:
        return tuple(
            attempt
            for attempt in self.attempts
            if attempt.execution is not None
            and not attempt.execution.oracle_comparison.matches
        )

    @property
    def exit_code(self) -> int:
        if self.suite_error_type is not None or self.failures:
            return 2
        if self.mismatches:
            return 1
        return 0


def _git_state(repo_root: Path) -> tuple[str | None, bool]:
    commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain=v1", "--untracked-files=normal"),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    resolved_commit = commit.stdout.strip() if commit.returncode == 0 else None
    dirty = status.returncode != 0 or bool(status.stdout.strip())
    return resolved_commit, dirty


def _selected_cases(
    suite: ControlledSuite,
    task_ids: tuple[str, ...] | None,
    strategy_ids: tuple[str, ...] | None,
) -> tuple[ControlledCase, ...]:
    tasks = task_ids if task_ids is not None else suite.manifest.task_ids
    strategies = strategy_ids if strategy_ids is not None else suite.manifest.strategy_ids
    if len(set(tasks)) != len(tasks):
        raise PilotTaskError("task selection contains duplicate IDs")
    if len(set(strategies)) != len(strategies):
        raise PilotTaskError("strategy selection contains duplicate IDs")
    unknown_tasks = set(tasks) - set(suite.manifest.task_ids)
    unknown_strategies = set(strategies) - set(suite.manifest.strategy_ids)
    if unknown_tasks:
        raise PilotTaskError("unknown task(s): " + ", ".join(sorted(unknown_tasks)))
    if unknown_strategies:
        raise PilotTaskError("unknown strategy(s): " + ", ".join(sorted(unknown_strategies)))
    return tuple(ControlledCase(task_id=task, strategy_id=strategy) for task in tasks for strategy in strategies)


def _existing_artifacts(paths: tuple[Path, ...], output_root: Path) -> tuple[ArtifactIntegrity, ...]:
    return tuple(
        artifact_integrity(path, output_root=output_root) for path in paths if path.is_file()
    )


def run_controlled_suite_experiment(
    suite: ControlledSuite,
    *,
    output_root: str | Path,
    repo_root: str | Path,
    image: str,
    rebuild: bool = False,
    task_ids: tuple[str, ...] | None = None,
    strategy_ids: tuple[str, ...] | None = None,
    workspace_parent: str | Path | None = None,
    run_id: str | None = None,
    supersedes_run_id: str | None = None,
    image_resolver: ImageResolver = ensure_linux_provenance_image,
    strategy_executor: StrategyExecutor = run_frozen_strategy_in_container,
) -> ControlledExperimentResult:
    """Plan every case, then capture all possible cases without fail-fast loss."""
    root = Path(output_root).resolve()
    code_root = Path(repo_root).resolve()
    cases = _selected_cases(suite, task_ids, strategy_ids)
    if not cases:
        raise PilotTaskError("controlled experiment selection is empty")
    if root.exists():
        raise PilotTaskError(f"refusing to reuse controlled experiment output directory: {root}")

    resolved_run_id = run_id or f"controlled_{uuid.uuid4().hex}"
    git_commit, git_dirty = _git_state(code_root)
    manifest = ControlledRunManifest(
        run_id=resolved_run_id,
        suite_id=suite.manifest.suite_id,
        suite_role=suite.manifest.resolved_role,
        created_at=utc_now(),
        suite_manifest_sha256=sha256_path(suite.root / "suite.json"),
        selected_cases=cases,
        image_reference=image,
        rebuild_requested=rebuild,
        git_commit=git_commit,
        git_dirty=git_dirty,
        host_platform=platform.platform(),
        host_architecture=platform.machine() or "unknown",
        python_version=platform.python_version(),
        supersedes_run_id=supersedes_run_id,
    )

    root.mkdir(parents=True, exist_ok=False)
    manifest_path = root / "run-manifest.json"
    ledger_path = root / manifest.ledger_file
    write_run_manifest(manifest, manifest_path)
    ledger = AppendOnlyExperimentLedger(
        ledger_path,
        run_id=manifest.run_id,
        suite_id=manifest.suite_id,
    )
    ledger.append(
        "suite_planned",
        artifacts=(artifact_integrity(manifest_path, output_root=root),),
        details={
            "planned_case_count": len(cases),
            "suite_role": manifest.suite_role,
            "suite_manifest_sha256": manifest.suite_manifest_sha256,
        },
    )
    # Freeze the complete intent set before image resolution or any evaluated command.
    for case in cases:
        ledger.append(
            "case_planned",
            task_id=case.task_id,
            strategy_id=case.strategy_id,
            details={"attempt_policy": "single_primary_attempt"},
        )

    try:
        container_digest = image_resolver(
            image=image,
            repo_root=code_root,
            rebuild=rebuild,
        )
    except Exception as exc:
        ledger.append(
            "suite_failed",
            details={
                "phase": "image_resolution",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "completed_case_count": 0,
                "failed_case_count": 0,
                "unattempted_case_count": len(cases),
            },
        )
        verify_experiment_ledger(ledger_path)
        return ControlledExperimentResult(
            manifest=manifest,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            container_digest=None,
            attempts=(),
            suite_error_type=type(exc).__name__,
            suite_error_message=str(exc),
        )

    ledger.append(
        "image_resolved",
        details={"image_reference": image, "container_digest": container_digest},
    )
    attempts: list[ControlledCaseAttempt] = []
    for case in cases:
        task = suite.task(case.task_id)
        strategy = task.oracle.strategies[case.strategy_id]
        assert strategy.fixture_variant is not None
        variant = task.oracle.variants[strategy.fixture_variant]
        raw_certificate = root / case.task_id / f"{case.strategy_id}.raw.certificate.json"
        gate_decision = root / case.task_id / f"{case.strategy_id}.gate.json"
        metrics = root / case.task_id / f"{case.strategy_id}.metrics.json"
        paths = (raw_certificate, gate_decision, metrics)
        ledger.append(
            "attempt_started",
            task_id=case.task_id,
            strategy_id=case.strategy_id,
            attempt=1,
            details={
                "fixture_variant": strategy.fixture_variant,
                "frozen_command": list(variant.command),
                "container_digest": container_digest,
            },
        )
        try:
            execution = strategy_executor(
                task,
                case.strategy_id,
                container_digest=container_digest,
                raw_certificate_path=raw_certificate,
                gate_decision_path=gate_decision,
                metrics_path=metrics,
                workspace_parent=workspace_parent,
            )
            artifacts = _existing_artifacts(paths, root)
            if len(artifacts) != 3:
                raise PilotTaskError(
                    f"{case.task_id}/{case.strategy_id} did not retain all three artifacts"
                )
            capture = execution.raw_certificate.command_captures[0]
            linux_events = capture.linux_events
            if linux_events is None:
                raise PilotTaskError(
                    f"{case.task_id}/{case.strategy_id} has no normalized Linux events"
                )
            ledger.append(
                "attempt_completed",
                task_id=case.task_id,
                strategy_id=case.strategy_id,
                attempt=1,
                artifacts=artifacts,
                details={
                    "fixture_variant": execution.variant_id,
                    "trace_id": execution.raw_certificate.trace_id,
                    "collector": capture.collector,
                    "parser_profile": "crucible-linux-strace-v1",
                    "strace_version": linux_events.strace_version,
                    "command_started_at_s": capture.started_at,
                    "command_finished_at_s": capture.finished_at,
                    "evidence_status": execution.gate_decision.evidence_status,
                    "scientific_status": execution.gate_decision.scientific_status,
                    "reason_code": execution.gate_decision.reason_code,
                    "oracle_match": execution.oracle_comparison.matches,
                    "oracle_mismatched_fields": list(
                        execution.oracle_comparison.mismatched_fields
                    ),
                    "metrics": execution.metrics.model_dump(mode="json"),
                },
            )
            attempts.append(
                ControlledCaseAttempt(
                    case=case,
                    execution=execution,
                    artifacts=artifacts,
                )
            )
        except Exception as exc:
            artifacts = _existing_artifacts(paths, root)
            ledger.append(
                "attempt_failed",
                task_id=case.task_id,
                strategy_id=case.strategy_id,
                attempt=1,
                artifacts=artifacts,
                details={
                    "phase": "capture_gate_or_retention",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "retained_artifact_count": len(artifacts),
                },
            )
            attempts.append(
                ControlledCaseAttempt(
                    case=case,
                    execution=None,
                    artifacts=artifacts,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )

    failures = sum(not attempt.succeeded for attempt in attempts)
    mismatches = sum(
        attempt.execution is not None and not attempt.execution.oracle_comparison.matches
        for attempt in attempts
    )
    ledger.append(
        "suite_completed",
        details={
            "planned_case_count": len(cases),
            "attempted_case_count": len(attempts),
            "completed_case_count": len(attempts) - failures,
            "failed_case_count": failures,
            "oracle_mismatch_count": mismatches,
            "container_digest": container_digest,
        },
    )
    verify_experiment_ledger(ledger_path)
    return ControlledExperimentResult(
        manifest=manifest,
        manifest_path=manifest_path,
        ledger_path=ledger_path,
        container_digest=container_digest,
        attempts=tuple(attempts),
    )


__all__ = [
    "ControlledCaseAttempt",
    "ControlledExperimentResult",
    "run_controlled_suite_experiment",
]
