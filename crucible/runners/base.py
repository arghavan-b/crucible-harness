"""Runner interface and implementations (design §15).

One interface over local execution and remote GPU (Modal / RunPod). The executor
is Runner-agnostic: it calls `run(...)` and never cares whether the command lands
in a Docker container, a host subprocess, or a remote GPU box.

Two implementations ship in the Stage-0 slice:
  - LocalSubprocessRunner: runs commands on the host inside a working dir. No
    isolation; for development and CI where Docker is unavailable.
  - LocalDockerRunner: runs commands inside a container (the real unit of
    isolation, design §6.3). Used once Docker is present.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol, runtime_checkable

from crucible.trace.capture import (
    CaptureCompleteness,
    CaptureFacet,
    CaptureState,
    CapturedCommandResult,
    MonitorContext,
    MonitoredCommandEnvelope,
    snapshot_regular_files,
)


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class MonitoredCommandResult:
    """A command outcome paired with the capture envelope for that invocation."""

    command: CommandResult | None
    capture: MonitoredCommandEnvelope


@runtime_checkable
class Runner(Protocol):
    def run(
        self, command: str, working_dir: str, timeout_s: int = 1800, image: str | None = None
    ) -> CommandResult: ...


@runtime_checkable
class MonitoredRunner(Runner, Protocol):
    """Harness-trusted capture backend.

    This object is part of the trusted computing base, not supplied by the
    evaluated workload. Response checks establish internal consistency; they
    cannot authenticate a malicious runner implementation.
    """

    def monitoring_trust_basis(self) -> Literal["harness_tcb"]: ...

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult: ...


class DockerContainerResolver(Protocol):
    def container_for(self, working_dir: str) -> str | None: ...


def _decoded_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def monitored_result_consistency_error(
    monitored: MonitoredCommandResult,
    *,
    command: str,
    context: MonitorContext,
    timeout_s: int,
    image: str | None,
) -> str | None:
    """Return an internal mismatch; this does not authenticate the trusted runner."""
    capture = monitored.capture
    if capture.context != context:
        return "capture context does not match the monitored request"
    if capture.submitted_command != command:
        return "captured command does not match the monitored request"
    if capture.timeout_s != timeout_s:
        return "captured timeout does not match the monitored request"
    if capture.image != image:
        return "captured image does not match the monitored request"

    result = monitored.command
    observed = capture.result
    if observed.outcome == "runner_error":
        if result is not None:
            return "runner-error capture must not fabricate a process result"
        return None
    if result is None:
        return "completed or timed-out capture is missing its process result"
    if observed.exit_code != result.exit_code or observed.timed_out != result.timed_out:
        return "captured outcome disagrees with the process result"
    if observed.stdout_chars != len(result.stdout) or observed.stderr_chars != len(result.stderr):
        return "captured stdio lengths disagree with the process result"
    if observed.stdout_text_sha256 != _text_sha256(result.stdout):
        return "captured stdout digest disagrees with the process result"
    if observed.stderr_text_sha256 != _text_sha256(result.stderr):
        return "captured stderr digest disagrees with the process result"
    return None


def _capture_runner_call(
    runner: Runner,
    invoke: Callable[[], CommandResult],
    *,
    command: str,
    working_dir: str,
    context: MonitorContext,
    timeout_s: int,
    image: str | None,
) -> MonitoredCommandResult:
    """Capture an honest top-level command envelope around one Runner call."""
    started_at = time.time()
    envelope_started_monotonic = time.monotonic()
    before = snapshot_regular_files(working_dir)
    command_started_monotonic = time.monotonic()
    runner_error: str | None = None
    result: CommandResult | None
    try:
        result = invoke()
    except Exception as exc:  # preserve the capture even when a backend raises
        runner_error = f"{type(exc).__name__}: {exc}"
        result = None
    command_duration_s = time.monotonic() - command_started_monotonic
    after = snapshot_regular_files(working_dir)
    finished_at = time.time()

    if runner_error is not None:
        captured_result = CapturedCommandResult(
            outcome="runner_error",
            exit_code=None,
            timed_out=False,
            stdout_chars=0,
            stderr_chars=0,
            stdout_text_sha256=_text_sha256(""),
            stderr_text_sha256=_text_sha256(""),
            runner_error=runner_error,
            cleanup_status="unverified",
        )
    else:
        assert result is not None
        captured_result = CapturedCommandResult(
            outcome="timed_out" if result.timed_out else "completed",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_chars=len(result.stdout),
            stderr_chars=len(result.stderr),
            stdout_text_sha256=_text_sha256(result.stdout),
            stderr_text_sha256=_text_sha256(result.stderr),
            cleanup_status="unverified",
        )

    facets = {facet: CaptureState.UNSUPPORTED for facet in CaptureFacet}
    facets[CaptureFacet.SUBMITTED_COMMAND] = CaptureState.CAPTURED
    facets[CaptureFacet.COMMAND_RESULT] = (
        CaptureState.INCOMPLETE if runner_error is not None else CaptureState.CAPTURED
    )
    facets[CaptureFacet.DECODED_STDIO_TEXT] = (
        CaptureState.INCOMPLETE if runner_error is not None else CaptureState.CAPTURED
    )
    snapshots_final = (
        before.complete and after.complete and captured_result.cleanup_status != "unverified"
    )
    facets[CaptureFacet.PRE_POST_FILE_DIGESTS] = (
        CaptureState.CAPTURED if snapshots_final else CaptureState.INCOMPLETE
    )
    capture_issues: tuple[str, ...] = ()
    if runner_error is not None:
        capture_issues = (
            runner_error,
            "process cleanup could not be verified; the post-command snapshot is not final",
        )
    elif result is not None and result.timed_out:
        capture_issues = (
            "process cleanup could not be verified after timeout; "
            "the post-command snapshot is not final",
        )
    else:
        capture_issues = (
            "process-tree quiescence is not verified after top-level exit; "
            "the post-command snapshot may not be final",
        )
    completeness = CaptureCompleteness(
        facets=facets,
        issues=capture_issues,
    )
    capture = MonitoredCommandEnvelope(
        capture_id=context.capture_id,
        context=context,
        submitted_command=command,
        runner_type=f"{type(runner).__module__}.{type(runner).__qualname__}",
        host_platform=sys.platform,
        image=image,
        timeout_s=timeout_s,
        started_at=started_at,
        finished_at=finished_at,
        command_duration_s=command_duration_s,
        envelope_duration_s=time.monotonic() - envelope_started_monotonic,
        before=before,
        after=after,
        result=captured_result,
        completeness=completeness,
    )
    return MonitoredCommandResult(command=result, capture=capture)


class _HarnessMonitoredMixin:
    def monitoring_trust_basis(self) -> Literal["harness_tcb"]:
        return "harness_tcb"


class LocalSubprocessRunner(_HarnessMonitoredMixin):
    """Executes commands on the host. Dev/CI only — provides no isolation."""

    def run(
        self, command: str, working_dir: str, timeout_s: int = 1800, image: str | None = None
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            out = _decoded_timeout_output(exc.stdout)
            err = _decoded_timeout_output(exc.stderr)
            return CommandResult(
                exit_code=124,
                stdout=out,
                stderr=f"{err}\n[crucible] timed out after {timeout_s}s",
                timed_out=True,
            )
        return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        return _capture_runner_call(
            self,
            lambda: self.run(command, working_dir, timeout_s, image),
            command=command,
            working_dir=working_dir,
            context=context,
            timeout_s=timeout_s,
            image=image,
        )


class LocalDockerRunner(_HarnessMonitoredMixin):
    """Stateless: a fresh container per command (design §6.3).

    `image` selects the base image; `working_dir` is bind-mounted as the
    container workspace. State does NOT persist across commands (each `docker
    run` starts from the image), so this suits one-off commands, not multi-step
    plans that install dependencies — use DockerExecRunner for those.
    """

    def run(
        self, command: str, working_dir: str, timeout_s: int = 1800, image: str | None = None
    ) -> CommandResult:
        if image is None:
            raise ValueError("LocalDockerRunner requires an image reference.")
        docker_cmd = (
            f"docker run --rm -v {shlex.quote(working_dir)}:/workspace -w /workspace "
            f"{shlex.quote(image)} /bin/sh -lc {shlex.quote(command)}"
        )
        try:
            proc = subprocess.run(
                docker_cmd, shell=True, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=124,
                stdout=_decoded_timeout_output(exc.stdout),
                stderr=(_decoded_timeout_output(exc.stderr) or f"timed out after {timeout_s}s"),
                timed_out=True,
            )
        return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        return _capture_runner_call(
            self,
            lambda: self.run(command, working_dir, timeout_s, image),
            command=command,
            working_dir=working_dir,
            context=context,
            timeout_s=timeout_s,
            image=image,
        )


class DockerExecRunner(_HarnessMonitoredMixin):
    """Runs each step with `docker exec` in the environment's persistent
    container, so installed dependencies persist across steps within a run. It
    resolves the container from the working dir via the environment manager, so
    the executor stays Runner-agnostic.
    """

    def __init__(self, envmgr: DockerContainerResolver) -> None:
        self.envmgr = envmgr

    def run(
        self, command: str, working_dir: str, timeout_s: int = 1800, image: str | None = None
    ) -> CommandResult:
        cid = self.envmgr.container_for(working_dir)
        if not cid:
            raise RuntimeError(f"no container provisioned for {working_dir!r}")
        cmd = ["docker", "exec", "-w", "/workspace", cid, "/bin/sh", "-lc", command]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=124,
                stdout=_decoded_timeout_output(exc.stdout),
                stderr=(_decoded_timeout_output(exc.stderr) or f"timed out after {timeout_s}s"),
                timed_out=True,
            )
        return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        return _capture_runner_call(
            self,
            lambda: self.run(command, working_dir, timeout_s, image),
            command=command,
            working_dir=working_dir,
            context=context,
            timeout_s=timeout_s,
            image=image,
        )


__all__ = [
    "CommandResult",
    "DockerExecRunner",
    "LocalDockerRunner",
    "LocalSubprocessRunner",
    "MonitoredCommandResult",
    "MonitoredRunner",
    "Runner",
    "monitored_result_consistency_error",
]
