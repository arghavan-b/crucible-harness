"""Typed capture envelopes for monitored scientific and recovery command submissions.

The v1 envelope improves auditability without pretending that snapshots prove
causal provenance. It captures the runner call, decoded stdio digests, and
regular-file hashes immediately before and after the call. Process identities,
parentage, file reads, write episodes, and renames remain explicitly unsupported
until a Linux event collector supplies them. Because top-level exit does not
establish process-tree quiescence, v1 post-command snapshots remain incomplete.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, Self, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_serializer,
    field_validator,
    model_validator,
)


_Key = TypeVar("_Key")
_Value = TypeVar("_Value")


class _FrozenMapping(Mapping[_Key, _Value]):
    """A read-only mapping that cannot be mutated through ``dict`` methods."""

    __slots__ = ("_data",)
    _data: Mapping[_Key, _Value]

    def __init__(self, value: Mapping[_Key, _Value]) -> None:
        object.__setattr__(self, "_data", MappingProxyType(dict(value)))

    def __getitem__(self, key: _Key) -> _Value:
        return self._data[key]

    def __iter__(self) -> Iterator[_Key]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("capture mappings are immutable")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, _memo: dict[int, object]) -> Self:
        # Capture maps contain only strings/enums; sharing is safe because every
        # mutator is blocked.
        return self


def _freeze_mapping(value: Mapping[_Key, _Value]) -> Mapping[_Key, _Value]:
    return _FrozenMapping(value)


class CaptureState(str, Enum):
    CAPTURED = "captured"
    INCOMPLETE = "incomplete"
    UNSUPPORTED = "unsupported"
    NOT_REQUESTED = "not_requested"


class CaptureFacet(str, Enum):
    SUBMITTED_COMMAND = "submitted_command"
    COMMAND_RESULT = "command_result"
    DECODED_STDIO_TEXT = "decoded_stdio_text"
    PRE_POST_FILE_DIGESTS = "pre_post_file_digests"
    PROCESS_IDENTITIES = "process_identities"
    PROCESS_PARENTAGE = "process_parentage"
    FILE_READS = "file_reads"
    FILE_WRITE_EPISODES = "file_write_episodes"
    FILE_RENAMES = "file_renames"


ALL_CAPTURE_FACETS = frozenset(CaptureFacet)
_V1_UNSUPPORTED_FACETS = frozenset(
    {
        CaptureFacet.PROCESS_IDENTITIES,
        CaptureFacet.PROCESS_PARENTAGE,
        CaptureFacet.FILE_READS,
        CaptureFacet.FILE_WRITE_EPISODES,
        CaptureFacet.FILE_RENAMES,
    }
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _StrictCaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MonitorContext(_StrictCaptureModel):
    trace_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    attempt: StrictInt = Field(ge=0)
    role: Literal["scientific_action", "recovery_action"] = "scientific_action"

    @property
    def capture_id(self) -> str:
        return f"{self.trace_id}:{self.step_id}:{self.attempt}:{self.role}"


class CaptureCompleteness(_StrictCaptureModel):
    facets: Mapping[CaptureFacet, CaptureState]
    issues: tuple[str, ...] = ()

    @field_validator("facets", mode="after")
    @classmethod
    def _freeze_facets(
        cls, value: Mapping[CaptureFacet, CaptureState]
    ) -> Mapping[CaptureFacet, CaptureState]:
        return _freeze_mapping(value)

    @field_serializer("facets")
    def _serialize_facets(self, value: Mapping[CaptureFacet, CaptureState]) -> dict[str, str]:
        return {facet.value: state.value for facet, state in value.items()}

    @model_validator(mode="after")
    def _all_facets_are_explicit(self) -> CaptureCompleteness:
        if set(self.facets) != ALL_CAPTURE_FACETS:
            missing = sorted(facet.value for facet in ALL_CAPTURE_FACETS - set(self.facets))
            extra = sorted(str(facet) for facet in set(self.facets) - ALL_CAPTURE_FACETS)
            raise ValueError(
                f"capture facets must be exhaustive (missing={missing}, extra={extra})"
            )
        return self


class WorkspaceDigestSnapshot(_StrictCaptureModel):
    files: Mapping[str, str]
    complete: StrictBool
    issues: tuple[str, ...] = ()

    @field_validator("files", mode="after")
    @classmethod
    def _freeze_files(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return _freeze_mapping(value)

    @field_serializer("files")
    def _serialize_files(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_snapshot(self) -> WorkspaceDigestSnapshot:
        for path, digest in self.files.items():
            pure = PurePosixPath(path)
            if not path or path == "." or "\\" in path or pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"unsafe snapshot path {path!r}")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"invalid SHA-256 for snapshot path {path!r}")
        if self.complete == bool(self.issues):
            raise ValueError(
                "complete snapshots must have no issues; incomplete snapshots need issues"
            )
        return self


class CapturedCommandResult(_StrictCaptureModel):
    outcome: Literal["completed", "timed_out", "runner_error"]
    exit_code: StrictInt | None
    timed_out: StrictBool
    stdout_chars: StrictInt = Field(ge=0)
    stderr_chars: StrictInt = Field(ge=0)
    stdout_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_error: str | None = None
    cleanup_status: Literal["verified", "unverified"]

    @model_validator(mode="after")
    def _consistent_outcome(self) -> CapturedCommandResult:
        if self.outcome == "runner_error":
            if self.exit_code is not None or self.timed_out or not self.runner_error:
                raise ValueError("runner_error outcomes require only runner_error detail")
        elif self.exit_code is None or self.runner_error is not None:
            raise ValueError(
                "completed/timed_out outcomes require an exit code and no runner error"
            )
        if (self.outcome == "timed_out") is not self.timed_out:
            raise ValueError("timed_out must agree with outcome")
        return self


class MonitoredCommandEnvelope(_StrictCaptureModel):
    schema_version: Literal[1] = 1
    capture_id: str = Field(min_length=1)
    collector: Literal["crucible-command-envelope-v1"] = "crucible-command-envelope-v1"
    scope: Literal["top_level_runner_call_only"] = "top_level_runner_call_only"
    trust_basis: Literal["runner_is_harness_tcb"] = "runner_is_harness_tcb"
    context: MonitorContext
    submitted_command: str = Field(min_length=1)
    logical_working_dir: Literal["."] = "."
    runner_type: str = Field(min_length=1)
    host_platform: str = Field(min_length=1)
    image: str | None = None
    timeout_s: StrictInt = Field(gt=0)
    started_at: float
    finished_at: float
    command_duration_s: float = Field(ge=0.0)
    envelope_duration_s: float = Field(ge=0.0)
    before: WorkspaceDigestSnapshot
    after: WorkspaceDigestSnapshot
    result: CapturedCommandResult
    completeness: CaptureCompleteness

    @model_validator(mode="after")
    def _consistent_envelope(self) -> MonitoredCommandEnvelope:
        if self.capture_id != self.context.capture_id:
            raise ValueError("capture_id must be derived from the monitor context")
        if self.finished_at < self.started_at:
            raise ValueError("capture finish time precedes its start time")
        facets = self.completeness.facets
        if facets[CaptureFacet.SUBMITTED_COMMAND] is not CaptureState.CAPTURED:
            raise ValueError("the v1 envelope must capture the submitted command")
        expected_result_state = (
            CaptureState.INCOMPLETE
            if self.result.outcome == "runner_error"
            else CaptureState.CAPTURED
        )
        if facets[CaptureFacet.COMMAND_RESULT] is not expected_result_state:
            raise ValueError("command-result completeness disagrees with the captured outcome")
        if facets[CaptureFacet.DECODED_STDIO_TEXT] is not expected_result_state:
            raise ValueError("stdio completeness disagrees with the captured outcome")
        snapshots_final = (
            self.before.complete
            and self.after.complete
            and self.result.cleanup_status != "unverified"
        )
        expected_snapshot_state = (
            CaptureState.CAPTURED if snapshots_final else CaptureState.INCOMPLETE
        )
        if facets[CaptureFacet.PRE_POST_FILE_DIGESTS] is not expected_snapshot_state:
            raise ValueError("digest completeness disagrees with snapshot or cleanup status")
        if any(facets[facet] is not CaptureState.UNSUPPORTED for facet in _V1_UNSUPPORTED_FACETS):
            raise ValueError("the v1 command envelope cannot claim causal provenance facets")
        return self


class RunCaptureSummary(_StrictCaptureModel):
    mode: Literal["not_requested", "no_action", "command_envelope_v1", "partial", "unavailable"]
    capture_ids: tuple[str, ...]
    capture_count: StrictInt = Field(ge=0)
    completed_commands: StrictInt = Field(ge=0)
    timed_out_commands: StrictInt = Field(ge=0)
    runner_errors: StrictInt = Field(ge=0)
    facets: Mapping[CaptureFacet, CaptureState]
    issues: tuple[str, ...] = ()

    @field_validator("facets", mode="after")
    @classmethod
    def _freeze_facets(
        cls, value: Mapping[CaptureFacet, CaptureState]
    ) -> Mapping[CaptureFacet, CaptureState]:
        return _freeze_mapping(value)

    @field_serializer("facets")
    def _serialize_facets(self, value: Mapping[CaptureFacet, CaptureState]) -> dict[str, str]:
        return {facet.value: state.value for facet, state in value.items()}

    @model_validator(mode="after")
    def _summary_is_consistent(self) -> RunCaptureSummary:
        if set(self.facets) != ALL_CAPTURE_FACETS:
            raise ValueError("run capture summary must report every capture facet")
        if self.capture_count != len(self.capture_ids):
            raise ValueError("capture_count must equal the number of capture IDs")
        if len(set(self.capture_ids)) != len(self.capture_ids):
            raise ValueError("capture IDs must be unique")
        outcomes = self.completed_commands + self.timed_out_commands + self.runner_errors
        if outcomes != self.capture_count:
            raise ValueError("capture outcome counts must sum to capture_count")
        return self


def snapshot_regular_files(root: str | os.PathLike[str]) -> WorkspaceDigestSnapshot:
    """Hash every regular file without following symlinks; report all uncertainty."""
    root_path = Path(root)
    files: dict[str, str] = {}
    issues: list[str] = []
    try:
        root_stat = root_path.lstat()
    except OSError as exc:
        return WorkspaceDigestSnapshot(
            files={}, complete=False, issues=(f"workspace root unavailable: {exc}",)
        )
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return WorkspaceDigestSnapshot(
            files={}, complete=False, issues=("workspace root is not a regular directory",)
        )

    def on_walk_error(exc: OSError) -> None:
        issues.append(f"walk error: {exc}")

    for directory, directory_names, file_names in os.walk(
        root_path, topdown=True, followlinks=False, onerror=on_walk_error
    ):
        parent = Path(directory)
        for name in list(directory_names):
            path = parent / name
            relative = PurePosixPath(*path.relative_to(root_path).parts).as_posix()
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                issues.append(f"cannot inspect directory {relative}: {exc}")
                directory_names.remove(name)
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                issues.append(f"unsupported directory entry {relative}")
                directory_names.remove(name)

        for name in file_names:
            path = parent / name
            relative = PurePosixPath(*path.relative_to(root_path).parts).as_posix()
            if "\\" in relative:
                issues.append(f"unsupported file path {relative!r}")
                continue
            try:
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    issues.append(f"unsupported file entry {relative}")
                    continue
                digest = _sha256_file(path)
                after = path.lstat()
            except OSError as exc:
                issues.append(f"cannot hash {relative}: {exc}")
                continue
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after or not stat.S_ISREG(after.st_mode):
                issues.append(f"file changed while hashing {relative}")
                continue
            files[relative] = digest

    return WorkspaceDigestSnapshot(
        files=dict(sorted(files.items())), complete=not issues, issues=tuple(issues)
    )


def summarize_captures(
    captures: list[MonitoredCommandEnvelope],
    *,
    monitoring_requested: bool,
    monitoring_failures: tuple[tuple[CaptureState, str], ...] = (),
) -> RunCaptureSummary:
    if not captures:
        failure_state = monitoring_failures[0][0] if monitoring_failures else None
        state = failure_state or CaptureState.NOT_REQUESTED
        mode: Literal["not_requested", "no_action", "unavailable"]
        issues: tuple[str, ...]
        if monitoring_failures:
            mode = "unavailable"
            issues = tuple(reason for _state, reason in monitoring_failures)
        elif monitoring_requested:
            mode = "no_action"
            issues = ("no eligible scientific action reached the monitored execution boundary",)
        else:
            mode = "not_requested"
            issues = ()
        return RunCaptureSummary(
            mode=mode,
            capture_ids=(),
            capture_count=0,
            completed_commands=0,
            timed_out_commands=0,
            runner_errors=0,
            facets={
                facet: (
                    CaptureState.UNSUPPORTED
                    if monitoring_failures and facet in _V1_UNSUPPORTED_FACETS
                    else state
                )
                for facet in CaptureFacet
            },
            issues=issues,
        )

    def aggregate(facet: CaptureFacet) -> CaptureState:
        states = {capture.completeness.facets[facet] for capture in captures}
        if facet not in _V1_UNSUPPORTED_FACETS:
            states.update(state for state, _reason in monitoring_failures)
        for candidate in (
            CaptureState.INCOMPLETE,
            CaptureState.UNSUPPORTED,
            CaptureState.NOT_REQUESTED,
            CaptureState.CAPTURED,
        ):
            if candidate in states:
                return candidate
        raise AssertionError("capture state aggregation received no states")

    outcomes = [capture.result.outcome for capture in captures]
    has_incomplete_capture = any(
        CaptureState.INCOMPLETE in capture.completeness.facets.values() for capture in captures
    )
    return RunCaptureSummary(
        mode=(
            "partial" if monitoring_failures or has_incomplete_capture else "command_envelope_v1"
        ),
        capture_ids=tuple(capture.capture_id for capture in captures),
        capture_count=len(captures),
        completed_commands=outcomes.count("completed"),
        timed_out_commands=outcomes.count("timed_out"),
        runner_errors=outcomes.count("runner_error"),
        facets={facet: aggregate(facet) for facet in CaptureFacet},
        issues=tuple(
            [reason for _state, reason in monitoring_failures]
            + [
                issue
                for capture in captures
                for issue in (
                    *capture.before.issues,
                    *capture.after.issues,
                    *capture.completeness.issues,
                )
            ]
        ),
    )


__all__ = [
    "ALL_CAPTURE_FACETS",
    "CaptureCompleteness",
    "CaptureFacet",
    "CaptureState",
    "CapturedCommandResult",
    "MonitorContext",
    "MonitoredCommandEnvelope",
    "RunCaptureSummary",
    "WorkspaceDigestSnapshot",
    "snapshot_regular_files",
    "summarize_captures",
]
