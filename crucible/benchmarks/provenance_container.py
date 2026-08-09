"""Host-side orchestration for frozen commands in the Linux provenance image."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from crucible.certificate import load_certificate
from crucible.schemas import ReproducibilityCertificate
from crucible.schemas.provenance import ProvenanceGateDecision

from .provenance import (
    ControlledTask,
    OracleComparison,
    PilotTaskError,
    clean_strategy_workspace,
    compare_gate_decision_to_oracle,
)
from .provenance_gate import evaluate_provenance

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class ProvenanceRunMetrics(BaseModel):
    """Performance measurements retained for one frozen strategy execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    runtime_s: float = Field(ge=0.0)
    trace_size_bytes: StrictInt = Field(ge=0)
    event_count: StrictInt = Field(ge=0)
    gate_latency_s: float = Field(ge=0.0)


@dataclass(frozen=True)
class LinuxContainerExecution:
    task_id: str
    strategy_id: str
    variant_id: str
    frozen_command: tuple[str, ...]
    raw_certificate_path: Path
    raw_certificate: ReproducibilityCertificate
    gate_decision_path: Path
    gate_decision: ProvenanceGateDecision
    oracle_comparison: OracleComparison
    metrics_path: Path
    metrics: ProvenanceRunMetrics
    container_digest: str
    stdout: str
    stderr: str


def build_linux_capture_argv(
    *,
    image: str,
    workspace: str | Path,
    certificate_directory: str | Path,
    experiment_id: str,
    frozen_command: Sequence[str],
    timeout_s: int,
    container_digest: str,
    user: str | None = None,
) -> tuple[str, ...]:
    """Build the Docker argv without shell interpolation or trusted-data mounts."""
    root = Path(workspace).resolve()
    output = Path(certificate_directory).resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"workspace must be a regular directory: {root}")
    output.mkdir(parents=True, exist_ok=True)
    command = tuple(frozen_command)
    if not command or any(not token or "\x00" in token for token in command):
        raise ValueError("frozen command must contain non-empty, NUL-free argv tokens")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")

    container_user = user
    if container_user is None and hasattr(os, "getuid") and hasattr(os, "getgid"):
        container_user = f"{os.getuid()}:{os.getgid()}"

    argv = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--cap-add",
        "SYS_PTRACE",
        "--security-opt",
        "seccomp=unconfined",
        "--network",
        "none",
        "--env",
        "HOME=/tmp",
        "--env",
        "CRUCIBLE_NETWORK_POLICY=none",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONHASHSEED=0",
        "--mount",
        f"type=bind,source={root},target=/experiment",
        "--mount",
        f"type=bind,source={output},target=/output",
        "--workdir",
        "/experiment",
    ]
    if container_user is not None:
        argv.extend(("--user", container_user))
    argv.extend(
        (
            image,
            "provenance-capture",
            "/experiment",
            "--experiment-id",
            experiment_id,
            "--command-json",
            json.dumps(command, separators=(",", ":")),
            "--out",
            "/output/certificate.json",
            "--timeout-s",
            str(timeout_s),
            "--container-digest",
            container_digest,
        )
    )
    return tuple(argv)


def _capture_matches_frozen_command(
    submitted_command: str,
    frozen_command: tuple[str, ...],
) -> bool:
    try:
        submitted = tuple(shlex.split(submitted_command))
    except ValueError:
        return False
    if not submitted or not frozen_command:
        return False
    if frozen_command[0] == "{python}":
        return "python" in PurePosixPath(submitted[0]).name and submitted[1:] == frozen_command[1:]
    return submitted == frozen_command


def run_frozen_strategy_in_container(
    task: ControlledTask,
    strategy_id: str,
    *,
    container_digest: str,
    raw_certificate_path: str | Path,
    gate_decision_path: str | Path,
    metrics_path: str | Path,
    workspace_parent: str | Path | None = None,
    run_command: RunCommand = subprocess.run,
) -> LinuxContainerExecution:
    """Capture and gate one strategy while retaining the untouched raw evidence."""
    destination = Path(raw_certificate_path).resolve()
    decision_destination = Path(gate_decision_path).resolve()
    metrics_destination = Path(metrics_path).resolve()
    destinations = (destination, decision_destination, metrics_destination)
    if len(set(destinations)) != len(destinations):
        raise PilotTaskError("raw certificate, gate decision, and metrics paths must differ")
    existing = [path for path in destinations if path.exists()]
    if existing:
        raise PilotTaskError(
            "refusing to overwrite retained provenance artifact(s): "
            + ", ".join(str(path) for path in existing)
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    decision_destination.parent.mkdir(parents=True, exist_ok=True)
    metrics_destination.parent.mkdir(parents=True, exist_ok=True)

    experiment_id = f"exp_{task.task_id}_{uuid.uuid4().hex[:12]}"
    with clean_strategy_workspace(
        task,
        strategy_id,
        workspace_parent=workspace_parent,
    ) as strategy_workspace:
        task.materialize(strategy_workspace.root)
        with tempfile.TemporaryDirectory(prefix="crucible_linux_capture_") as staging_text:
            staging = Path(staging_text)
            argv = build_linux_capture_argv(
                image=container_digest,
                workspace=strategy_workspace.root,
                certificate_directory=staging,
                experiment_id=experiment_id,
                frozen_command=strategy_workspace.command,
                timeout_s=task.contract.runtime.timeout_s,
                container_digest=container_digest,
            )
            completed = run_command(argv, check=False, capture_output=True, text=True)
            staged_certificate = staging / "certificate.json"
            if completed.returncode != 0:
                raise PilotTaskError(
                    f"{task.task_id}/{strategy_id} Linux provenance container exited "
                    f"{completed.returncode}: {completed.stderr.strip()}"
                )
            if not staged_certificate.is_file():
                raise PilotTaskError(f"{task.task_id}/{strategy_id} did not produce a certificate")
            certificate = load_certificate(str(staged_certificate))

    captures = certificate.command_captures
    if len(captures) != 1:
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} captured {len(captures)} commands; expected exactly one"
        )
    capture = captures[0]
    if capture.collector != "crucible-linux-strace-v1":
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} used unexpected collector {capture.collector!r}"
        )
    if capture.network_policy != "none":
        raise PilotTaskError(f"{task.task_id}/{strategy_id} did not attest no-network execution")
    if not _capture_matches_frozen_command(capture.submitted_command, strategy_workspace.command):
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} captured command does not match frozen argv"
        )
    if certificate.container_digest != container_digest:
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} certificate has the wrong container digest"
        )
    if certificate.provenance_adjudication != "not_performed":
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} container certificate was already adjudicated"
        )
    events = capture.linux_events
    if events is None or not events.raw_trace_size_bytes:
        raise PilotTaskError(
            f"{task.task_id}/{strategy_id} certificate does not record raw trace sizes"
        )

    # Persist the raw evidence before invoking the host-side gate. If gate
    # evaluation itself fails, the original capture remains available for
    # diagnosis and can never be confused with a gate-embedded certificate.
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(certificate.model_dump_json(indent=2))
    gate_started = time.perf_counter()
    decision = evaluate_provenance(task, certificate)
    gate_latency_s = time.perf_counter() - gate_started
    with decision_destination.open("x", encoding="utf-8") as handle:
        handle.write(decision.model_dump_json(indent=2))
    comparison = compare_gate_decision_to_oracle(task, strategy_id, decision)
    metrics = ProvenanceRunMetrics(
        task_id=task.task_id,
        strategy_id=strategy_id,
        trace_id=certificate.trace_id,
        runtime_s=capture.command_duration_s,
        trace_size_bytes=sum(events.raw_trace_size_bytes.values()),
        event_count=len(events.process_events) + len(events.file_events),
        gate_latency_s=gate_latency_s,
    )
    with metrics_destination.open("x", encoding="utf-8") as handle:
        handle.write(metrics.model_dump_json(indent=2))
    return LinuxContainerExecution(
        task_id=task.task_id,
        strategy_id=strategy_id,
        variant_id=strategy_workspace.variant_id,
        frozen_command=strategy_workspace.command,
        raw_certificate_path=destination,
        raw_certificate=certificate,
        gate_decision_path=decision_destination,
        gate_decision=decision,
        oracle_comparison=comparison,
        metrics_path=metrics_destination,
        metrics=metrics,
        container_digest=container_digest,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def ensure_linux_provenance_image(
    *,
    image: str,
    repo_root: str | Path,
    rebuild: bool = False,
    run_command: RunCommand = subprocess.run,
) -> str:
    """Build the Linux provenance image when requested or absent, then return its digest."""
    root = Path(repo_root).resolve()
    dockerfile = root / "docker" / "provenance.Dockerfile"
    inspect = run_command(
        ("docker", "image", "inspect", image),
        check=False,
        capture_output=True,
        text=True,
    )
    if rebuild or inspect.returncode != 0:
        built = run_command(
            (
                "docker",
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                image,
                str(root),
            ),
            check=False,
            capture_output=False,
            text=True,
        )
        if built.returncode != 0:
            raise PilotTaskError(f"failed to build Linux provenance image {image!r}")
    digest_result = run_command(
        ("docker", "image", "inspect", "--format={{.Id}}", image),
        check=False,
        capture_output=True,
        text=True,
    )
    digest = digest_result.stdout.strip()
    if digest_result.returncode != 0 or not digest.startswith("sha256:"):
        raise PilotTaskError(f"cannot resolve digest for Linux provenance image {image!r}")
    return digest


__all__ = [
    "LinuxContainerExecution",
    "ProvenanceRunMetrics",
    "build_linux_capture_argv",
    "ensure_linux_provenance_image",
    "run_frozen_strategy_in_container",
]
