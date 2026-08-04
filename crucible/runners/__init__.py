"""Command-runner interfaces and built-in execution backends."""

from .base import (
    CommandResult,
    DockerExecRunner,
    LocalDockerRunner,
    LocalSubprocessRunner,
    LinuxStraceRunner,
    MonitoredCommandResult,
    MonitoredRunner,
    Runner,
    monitored_result_consistency_error,
)

__all__ = [
    "CommandResult",
    "DockerExecRunner",
    "LocalDockerRunner",
    "LocalSubprocessRunner",
    "LinuxStraceRunner",
    "MonitoredCommandResult",
    "MonitoredRunner",
    "Runner",
    "monitored_result_consistency_error",
]
