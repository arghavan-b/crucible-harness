"""Immutable run metadata and a hash-chained intent-to-evaluate ledger."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt

from .provenance import SuiteRole

LedgerEventType = Literal[
    "suite_planned",
    "case_planned",
    "image_resolved",
    "attempt_started",
    "attempt_completed",
    "attempt_failed",
    "suite_completed",
    "suite_failed",
]


def utc_now() -> str:
    """Return an unambiguous UTC timestamp for retained experiment records."""
    return datetime.now(timezone.utc).isoformat()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


class ControlledCase(_FrozenModel):
    task_id: str = Field(min_length=1)
    strategy_id: str = Field(min_length=1)


class ArtifactIntegrity(_FrozenModel):
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: StrictInt = Field(ge=0)


class ControlledRunManifest(_FrozenModel):
    """Write-once metadata known before the first evaluated command starts."""

    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    suite_id: str = Field(min_length=1)
    suite_role: SuiteRole
    created_at: str = Field(min_length=1)
    suite_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_cases: tuple[ControlledCase, ...]
    image_reference: str = Field(min_length=1)
    rebuild_requested: StrictBool
    network_policy: Literal["none"] = "none"
    git_commit: str | None = None
    git_dirty: StrictBool
    host_platform: str = Field(min_length=1)
    host_architecture: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    ledger_file: Literal["experiment-ledger.jsonl"] = "experiment-ledger.jsonl"
    supersedes_run_id: str | None = None


class ExperimentLedgerRecord(_FrozenModel):
    """One append-only, hash-chained transition in an experiment run."""

    schema_version: Literal[1] = 1
    sequence: StrictInt = Field(ge=0)
    run_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)
    event_type: LedgerEventType
    task_id: str | None = None
    strategy_id: str | None = None
    attempt: StrictInt | None = Field(default=None, ge=1)
    artifacts: tuple[ArtifactIntegrity, ...] = ()
    details: dict[str, JsonValue] = Field(default_factory=dict)
    previous_record_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_integrity(path: str | Path, *, output_root: str | Path) -> ArtifactIntegrity:
    target = Path(path).resolve()
    root = Path(output_root).resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"retained artifact escapes output root: {target}") from exc
    if not target.is_file():
        raise ValueError(f"retained artifact is not a regular file: {target}")
    return ArtifactIntegrity(
        relative_path=relative,
        sha256=sha256_path(target),
        size_bytes=target.stat().st_size,
    )


def _record_digest(data: Mapping[str, object]) -> str:
    payload = dict(data)
    payload.pop("record_sha256", None)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class AppendOnlyExperimentLedger:
    """Append durable JSONL records whose hashes bind the complete prior history."""

    def __init__(self, path: str | Path, *, run_id: str, suite_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.suite_id = suite_id
        self._sequence = 0
        self._previous: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8"):
            pass

    def append(
        self,
        event_type: LedgerEventType,
        *,
        task_id: str | None = None,
        strategy_id: str | None = None,
        attempt: int | None = None,
        artifacts: tuple[ArtifactIntegrity, ...] = (),
        details: Mapping[str, JsonValue] | None = None,
    ) -> ExperimentLedgerRecord:
        if (task_id is None) != (strategy_id is None):
            raise ValueError("ledger case events require both task_id and strategy_id")
        if event_type in {
            "case_planned",
            "attempt_started",
            "attempt_completed",
            "attempt_failed",
        } and task_id is None:
            raise ValueError(f"{event_type} requires a controlled case")
        if event_type.startswith("attempt_") and attempt is None:
            raise ValueError(f"{event_type} requires an attempt number")

        data: dict[str, object] = {
            "schema_version": 1,
            "sequence": self._sequence,
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "recorded_at": utc_now(),
            "event_type": event_type,
            "task_id": task_id,
            "strategy_id": strategy_id,
            "attempt": attempt,
            "artifacts": tuple(artifact.model_dump(mode="json") for artifact in artifacts),
            "details": dict(details or {}),
            "previous_record_sha256": self._previous,
            "record_sha256": "0" * 64,
        }
        data["record_sha256"] = _record_digest(data)
        record = ExperimentLedgerRecord.model_validate(data)
        line = record.model_dump_json() + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._sequence += 1
        self._previous = record.record_sha256
        return record


def verify_experiment_ledger(path: str | Path) -> tuple[ExperimentLedgerRecord, ...]:
    """Validate JSONL syntax, sequence continuity, and the complete hash chain."""
    records: list[ExperimentLedgerRecord] = []
    previous: str | None = None
    with Path(path).open(encoding="utf-8") as handle:
        for sequence, line in enumerate(handle):
            if not line.strip():
                raise ValueError(f"blank ledger record at line {sequence + 1}")
            record = ExperimentLedgerRecord.model_validate_json(line)
            if record.sequence != sequence:
                raise ValueError(
                    f"ledger sequence {record.sequence} does not equal expected {sequence}"
                )
            if record.previous_record_sha256 != previous:
                raise ValueError(f"ledger hash chain breaks at sequence {sequence}")
            actual = _record_digest(record.model_dump(mode="json"))
            if record.record_sha256 != actual:
                raise ValueError(f"ledger record digest mismatch at sequence {sequence}")
            records.append(record)
            previous = record.record_sha256
    if not records:
        raise ValueError("experiment ledger is empty")
    return tuple(records)


def write_run_manifest(manifest: ControlledRunManifest, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        handle.write(manifest.model_dump_json(indent=2) + "\n")


__all__ = [
    "AppendOnlyExperimentLedger",
    "ArtifactIntegrity",
    "ControlledCase",
    "ControlledRunManifest",
    "ExperimentLedgerRecord",
    "artifact_integrity",
    "sha256_path",
    "utc_now",
    "verify_experiment_ledger",
    "write_run_manifest",
]
