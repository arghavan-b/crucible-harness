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

import shlex
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class Runner(Protocol):
    def run(
        self, command: str, working_dir: str, timeout_s: int = 1800, image: str | None = None
    ) -> CommandResult: ...


class LocalSubprocessRunner:
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
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            err = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                exit_code=124,
                stdout=out,
                stderr=f"{err}\n[crucible] timed out after {timeout_s}s",
                timed_out=True,
            )
        return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class LocalDockerRunner:
    """Executes commands inside a Docker container (design §6.3).

    `image` selects the base image; `working_dir` is bind-mounted as the
    container workspace. Requires the docker CLI on PATH.
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
        except subprocess.TimeoutExpired:
            return CommandResult(
                exit_code=124, stdout="", stderr=f"timed out after {timeout_s}s", timed_out=True
            )
        return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
