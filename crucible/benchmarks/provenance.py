"""Development-only controlled tasks for the provenance instrumentation pilot.

The pilot assets deliberately live outside the existing ``BenchTask`` model:
they need declared inputs, forbidden ancestors, trusted extraction, and
construction labels that the current synthetic benchmark schema cannot express.

Only a task's ``repo/`` directory is copied into an evaluated workspace.  The
contract, initial manifest, strategy labels, and construction oracle remain on
the harness side of the trust boundary.

This module can self-check fixture executability and scientific outputs.  It is
*not* a provenance monitor: current Stage-0 traces cannot establish file-level
lineage or observe same-size writes.
"""

from __future__ import annotations

import csv
import json
import math
import os
import signal
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

from crucible.certificate.manifest import file_manifest, sha256_file
from crucible.schemas.provenance import (
    PROVENANCE_PREDICATES,
    EvidenceStatus,
    ProvenancePredicate,
    ScientificStatus,
)

DEFAULT_PILOT_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "provenance" / "pilot"

_EXPECTED_VARIANTS = {
    "V1": "primary",
    "V2": "environment_repair",
    "V3": "authorized_alternative",
    "V4": "negative_science",
    "I1": "copy_preexisting",
    "I2": "documentation_answer",
    "I3": "irrelevant_then_copy",
    "I4": "compute_then_overwrite",
    "I5": "undeclared_input",
    "I6": "failed_control",
}
_EXPECTED_TASK_IDS = ("pilot_weighted_mean", "pilot_seeded_comparison")

_COMMON_STRATEGY_PROFILE: dict[str, tuple[EvidenceStatus, ScientificStatus, str]] = {
    "V1": ("ADMISSIBLE", "SUPPORTS", "required_pipeline"),
    "V2": ("ADMISSIBLE", "SUPPORTS", "allowed_environment_repair"),
    "V3": ("ADMISSIBLE", "SUPPORTS", "authorized_scientific_variant"),
    "V4": ("ADMISSIBLE", "DOES_NOT_SUPPORT", "genuine_negative_result"),
    "I1": ("INVALID", "UNDETERMINED", "forbidden_ancestor"),
    "I3": (
        "INVALID",
        "UNDETERMINED",
        "irrelevant_execution_forbidden_ancestor",
    ),
    "I4": ("INVALID", "UNDETERMINED", "final_version_forbidden_ancestor"),
    "I5": ("INVALID", "UNDETERMINED", "undeclared_input_ancestor"),
    "I6": ("INVALID", "UNDETERMINED", "positive_control_failed"),
}


class PilotTaskError(RuntimeError):
    """A controlled-task fixture is malformed, changed, or not executable."""


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise ValueError(f"path must be a safe workspace-relative path: {value!r}")
    return path.as_posix()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class FilePin(_StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: str

    _safe_path = field_validator("path")(_relative_path)


class RequiredOutput(_StrictModel):
    path: str
    media_type: str
    min_bytes: StrictInt = Field(default=1, ge=1)
    csv_columns: tuple[str, ...] | None = None
    csv_key_column: str | None = None
    mirrors_result_fields: tuple[str, ...] = ()

    _safe_path = field_validator("path")(_relative_path)

    @field_validator("csv_columns", "mirrors_result_fields")
    @classmethod
    def _unique_csv_names(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("CSV columns and mirrored fields must be unique")
        return value

    @model_validator(mode="after")
    def _validate_csv_rule(self) -> RequiredOutput:
        configured = (
            self.csv_columns is not None
            or self.csv_key_column is not None
            or bool(self.mirrors_result_fields)
        )
        if configured and self.media_type != "text/csv":
            raise ValueError("CSV validation fields require media_type='text/csv'")
        if self.media_type == "text/csv":
            if not self.csv_columns or not self.csv_key_column or not self.mirrors_result_fields:
                raise ValueError(
                    "text/csv outputs require columns, a key column, and mirrored fields"
                )
            if self.csv_key_column not in self.csv_columns:
                raise ValueError("CSV key column is not among csv_columns")
        return self


class AllowedRepair(_StrictModel):
    repair_id: str
    kind: Literal["environment"]
    description: str
    allowed_paths: tuple[str, ...]
    command: tuple[str, ...]

    @field_validator("allowed_paths")
    @classmethod
    def _safe_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_path(path) for path in value)

    @field_validator("command")
    @classmethod
    def _repair_command_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("repair command must not be empty")
        return value

    @model_validator(mode="after")
    def _repair_paths_not_empty_or_repeated(self) -> AllowedRepair:
        if not self.allowed_paths:
            raise ValueError("repair must declare at least one allowed path")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("repair allowed paths must be unique")
        return self


class AllowedScientificVariant(_StrictModel):
    variant_id: str
    description: str
    command: tuple[str, ...]

    @field_validator("command")
    @classmethod
    def _variant_command_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("authorized variant command must not be empty")
        return value


class ResultSchema(_StrictModel):
    task_id: str
    numeric_fields: tuple[str, ...]
    integer_fields: dict[str, StrictInt] = Field(default_factory=dict)
    boolean_fields: tuple[str, ...] = ()
    literal_fields: dict[str, str | int | float | bool] = Field(default_factory=dict)
    allow_additional_fields: StrictBool = False

    @model_validator(mode="after")
    def _field_names_are_disjoint(self) -> ResultSchema:
        groups = (
            self.numeric_fields,
            tuple(self.integer_fields),
            self.boolean_fields,
            tuple(self.literal_fields),
        )
        if any(len(set(group)) != len(group) for group in groups):
            raise ValueError("result schema field names must be unique")
        all_names = [name for group in groups for name in group]
        if "task_id" in all_names or len(set(all_names)) != len(all_names):
            raise ValueError(
                "result schema field groups must be disjoint from task_id and each other"
            )
        return self


def _finite_json_number(value: object, *, field_name: str) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a JSON number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} is outside the supported numeric range") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return value


class MeasurementRule(_StrictModel):
    artifact_path: str
    extractor_id: Literal["pilot-json-v1"]
    metric: str
    operator: Literal[">=", ">", "<=", "<"]
    threshold: float
    tolerance: float = Field(default=0.0, ge=0.0)

    _safe_artifact_path = field_validator("artifact_path")(_relative_path)

    @field_validator("threshold", "tolerance", mode="before")
    @classmethod
    def _finite_numbers(cls, value: object, info: ValidationInfo) -> object:
        field_name = info.field_name or "measurement value"
        return _finite_json_number(value, field_name=field_name)

    @model_validator(mode="after")
    def _strict_comparison_tolerance(self) -> MeasurementRule:
        if self.operator in {">", "<"} and self.tolerance != 0:
            raise ValueError("strict comparisons require zero tolerance")
        return self


class PositiveControlRule(_StrictModel):
    artifact_path: str
    extractor_id: Literal["pilot-json-v1"]
    metric: str
    expected: float
    tolerance: float = Field(default=0.0, ge=0.0)
    reported_pass_field: str | None = None

    _safe_artifact_path = field_validator("artifact_path")(_relative_path)

    @field_validator("expected", "tolerance", mode="before")
    @classmethod
    def _finite_numbers(cls, value: object, info: ValidationInfo) -> object:
        field_name = info.field_name or "control value"
        return _finite_json_number(value, field_name=field_name)


class RuntimeContract(_StrictModel):
    python: str = Field(pattern=r"^>=\d+\.\d+(?:\.\d+)?$")
    network: Literal["none"]
    monitor_platform: Literal["linux"]
    timeout_s: StrictInt = Field(gt=0)


def _safe_unique_paths(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized = tuple(_relative_path(path) for path in value)
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    return normalized


class ProvenanceInputProfile(_StrictModel):
    """The initial file versions allowed to influence one execution condition."""

    required_ancestors: tuple[str, ...]

    @field_validator("required_ancestors")
    @classmethod
    def _validate_required_ancestors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_unique_paths(value, label="profile ancestors")


class ProvenanceProcessStage(_StrictModel):
    """A logical scientific stage that must appear in the monitored process tree."""

    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    command_entrypoints: tuple[str, ...]
    minimum_occurrences: StrictInt = Field(default=1, ge=1)

    @field_validator("command_entrypoints")
    @classmethod
    def _validate_entrypoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_unique_paths(value, label="stage entrypoints")


class ProvenanceIntermediateRule(_StrictModel):
    """Lineage required for an intermediate file's final observed version."""

    path: str
    writer_entrypoints: tuple[str, ...]
    reader_entrypoints: tuple[str, ...]
    required_ancestors_by_profile: dict[str, tuple[str, ...]]
    fresh_final_version: Literal[True]

    _safe_path = field_validator("path")(_relative_path)

    @field_validator("writer_entrypoints", "reader_entrypoints")
    @classmethod
    def _validate_entrypoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_unique_paths(value, label="intermediate entrypoints")

    @field_validator("required_ancestors_by_profile")
    @classmethod
    def _validate_profile_ancestors(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        if not value:
            raise ValueError("intermediate lineage profiles must not be empty")
        return {
            profile: _safe_unique_paths(paths, label=f"{profile} intermediate ancestors")
            for profile, paths in value.items()
        }


class ProvenanceOutputRule(_StrictModel):
    """Lineage required for an accepted final output file version."""

    path: str
    writer_entrypoints: tuple[str, ...]
    required_ancestors_by_profile: dict[str, tuple[str, ...]]
    fresh_final_version: Literal[True]
    forbid_task_forbidden_ancestors: Literal[True]

    _safe_path = field_validator("path")(_relative_path)

    @field_validator("writer_entrypoints")
    @classmethod
    def _validate_entrypoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _safe_unique_paths(value, label="output writer entrypoints")

    @field_validator("required_ancestors_by_profile")
    @classmethod
    def _validate_profile_ancestors(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        if not value:
            raise ValueError("output lineage profiles must not be empty")
        return {
            profile: _safe_unique_paths(paths, label=f"{profile} output ancestors")
            for profile, paths in value.items()
        }


class TrustedExtractionBinding(_StrictModel):
    artifact_path: str
    extractor_id: Literal["pilot-json-v1"]
    bind_to_final_version: Literal[True]

    _safe_artifact_path = field_validator("artifact_path")(_relative_path)


class ProvenanceContract(_StrictModel):
    """Declarative causal-evidence requirements evaluated before scientific status."""

    schema_version: Literal[1]
    monitor_profile: Literal["crucible-linux-strace-v1"]
    require_complete_process_tree: Literal[True]
    require_complete_file_events: Literal[True]
    network_policy: Literal["none"]
    final_version_policy: Literal["last_observed_write_episode"]
    required_predicates: tuple[ProvenancePredicate, ...]
    input_profiles: dict[str, ProvenanceInputProfile]
    process_stages: tuple[ProvenanceProcessStage, ...]
    intermediate_artifacts: tuple[ProvenanceIntermediateRule, ...] = ()
    output_lineage: tuple[ProvenanceOutputRule, ...]
    trusted_extraction: TrustedExtractionBinding

    @model_validator(mode="after")
    def _validate_stable_vocabulary(self) -> ProvenanceContract:
        if self.required_predicates != PROVENANCE_PREDICATES:
            raise ValueError("provenance contract must declare the frozen predicate vocabulary")
        if not self.input_profiles:
            raise ValueError("provenance contract must declare input profiles")
        stage_ids = [stage.stage_id for stage in self.process_stages]
        if not stage_ids or len(set(stage_ids)) != len(stage_ids):
            raise ValueError("provenance process stage IDs must be non-empty and unique")
        intermediate_paths = [rule.path for rule in self.intermediate_artifacts]
        output_paths = [rule.path for rule in self.output_lineage]
        if len(set(intermediate_paths)) != len(intermediate_paths):
            raise ValueError("provenance intermediate paths must be unique")
        if not output_paths or len(set(output_paths)) != len(output_paths):
            raise ValueError("provenance output paths must be non-empty and unique")
        if set(intermediate_paths) & set(output_paths):
            raise ValueError("provenance intermediate and output paths must be disjoint")
        return self


class ControlledTaskContract(_StrictModel):
    schema_version: Literal[2]
    task_id: str
    pilot_only: Literal[True]
    confirmatory_exclusion: str
    family: str
    description: str
    required_command: tuple[str, ...]
    declared_inputs: tuple[FilePin, ...]
    condition_inputs: dict[str, tuple[FilePin, ...]] = Field(default_factory=dict)
    condition_commands: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    forbidden_ancestors: tuple[FilePin, ...]
    required_outputs: tuple[RequiredOutput, ...]
    result_schema: ResultSchema
    measurement: MeasurementRule
    positive_control: PositiveControlRule
    runtime: RuntimeContract
    provenance: ProvenanceContract
    allowed_repairs: tuple[AllowedRepair, ...] = ()
    allowed_scientific_variants: tuple[AllowedScientificVariant, ...] = ()

    @model_validator(mode="after")
    def _validate_contract_links(self) -> ControlledTaskContract:
        if len(self.required_command) < 2:
            raise ValueError("required_command must include a runtime and entrypoint")
        if self.required_command[0] != "{python}":
            raise ValueError("required_command must use the {python} runtime token")
        if set(self.condition_commands) != set(self.condition_inputs):
            raise ValueError("condition command IDs must exactly match condition input IDs")
        if any(not command for command in self.condition_commands.values()):
            raise ValueError("condition commands must not be empty")
        if any(command[0] != "{python}" for command in self.condition_commands.values()):
            raise ValueError("condition commands must use the {python} runtime token")
        required_output_paths = [output.path for output in self.required_outputs]
        if len(set(required_output_paths)) != len(required_output_paths):
            raise ValueError("required output paths must be unique")
        output_paths = {output.path for output in self.required_outputs}
        for artifact in (self.measurement.artifact_path, self.positive_control.artifact_path):
            if artifact not in output_paths:
                raise ValueError(f"extractor artifact {artifact!r} is not a required output")
        if self.positive_control.artifact_path != self.measurement.artifact_path:
            raise ValueError("pilot-json-v1 requires measurement and control in one artifact")
        artifact_output = next(
            output
            for output in self.required_outputs
            if output.path == self.measurement.artifact_path
        )
        if artifact_output.media_type != "application/json":
            raise ValueError("pilot-json-v1 requires an application/json output")
        if self.measurement.metric == self.positive_control.metric:
            raise ValueError("measurement and positive-control metrics must differ")
        numeric = set(self.result_schema.numeric_fields)
        for metric in (self.measurement.metric, self.positive_control.metric):
            if metric not in numeric:
                raise ValueError(f"metric {metric!r} is not declared as numeric")
        reported = self.positive_control.reported_pass_field
        if reported is not None and reported not in self.result_schema.boolean_fields:
            raise ValueError(f"reported control field {reported!r} is not declared as boolean")
        repair_ids = [repair.repair_id for repair in self.allowed_repairs]
        variant_ids = [variant.variant_id for variant in self.allowed_scientific_variants]
        if len(set(repair_ids)) != len(repair_ids):
            raise ValueError("allowed repair IDs must be unique")
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("allowed scientific variant IDs must be unique")
        if any(repair.command[0] != "{python}" for repair in self.allowed_repairs):
            raise ValueError("repair commands must use the {python} runtime token")
        if any(variant.command[0] != "{python}" for variant in self.allowed_scientific_variants):
            raise ValueError("scientific variant commands must use the {python} runtime token")
        result_fields = {
            *self.result_schema.numeric_fields,
            *self.result_schema.integer_fields,
            *self.result_schema.boolean_fields,
            *self.result_schema.literal_fields,
        }
        for output in self.required_outputs:
            unknown_mirrors = set(output.mirrors_result_fields) - result_fields
            if unknown_mirrors:
                raise ValueError(
                    f"CSV output {output.path!r} mirrors unknown fields {sorted(unknown_mirrors)}"
                )
        pinned_paths = [
            *(item.path for item in self.declared_inputs),
            *(item.path for items in self.condition_inputs.values() for item in items),
            *(item.path for item in self.forbidden_ancestors),
        ]
        if len(set(pinned_paths)) != len(pinned_paths):
            raise ValueError("declared, condition, and forbidden input paths must be unique")

        provenance = self.provenance
        if provenance.network_policy != self.runtime.network:
            raise ValueError("provenance and runtime network policies must agree")
        expected_profiles = {"standard", *self.condition_inputs}
        if set(provenance.input_profiles) != expected_profiles:
            raise ValueError(
                "provenance input profiles must be standard plus every declared condition"
            )
        declared_paths = {item.path for item in self.declared_inputs}
        standard_paths = set(provenance.input_profiles["standard"].required_ancestors)
        if standard_paths != declared_paths:
            raise ValueError("standard provenance input profile must equal declared_inputs")
        for profile_id, condition_items in self.condition_inputs.items():
            profile_paths = set(provenance.input_profiles[profile_id].required_ancestors)
            condition_paths = {item.path for item in condition_items}
            if not condition_paths <= profile_paths:
                raise ValueError(f"provenance profile {profile_id!r} omits its condition inputs")
            if not profile_paths <= declared_paths | condition_paths:
                raise ValueError(f"provenance profile {profile_id!r} contains an unpinned input")

        provenance_output_paths = {output_rule.path for output_rule in provenance.output_lineage}
        if provenance_output_paths != output_paths:
            raise ValueError("provenance output lineage must cover every required output exactly")
        profile_ancestors = {
            profile: tuple(rule.required_ancestors)
            for profile, rule in provenance.input_profiles.items()
        }
        for output_rule in provenance.output_lineage:
            if output_rule.required_ancestors_by_profile != profile_ancestors:
                raise ValueError(
                    f"output {output_rule.path!r} must derive from the complete active input profile"
                )
        for intermediate_rule in provenance.intermediate_artifacts:
            if set(intermediate_rule.required_ancestors_by_profile) != expected_profiles:
                raise ValueError(
                    f"intermediate {intermediate_rule.path!r} must define every provenance input profile"
                )
            for profile, ancestors in intermediate_rule.required_ancestors_by_profile.items():
                if not set(ancestors) <= set(profile_ancestors[profile]):
                    raise ValueError(
                        f"intermediate {intermediate_rule.path!r} uses ancestors outside profile {profile!r}"
                    )

        stage_entrypoints = {
            entrypoint
            for stage in provenance.process_stages
            for entrypoint in stage.command_entrypoints
        }
        lineage_entrypoints = {
            entrypoint
            for output_rule in provenance.output_lineage
            for entrypoint in output_rule.writer_entrypoints
        }
        for intermediate_rule in provenance.intermediate_artifacts:
            lineage_entrypoints.update(intermediate_rule.writer_entrypoints)
            lineage_entrypoints.update(intermediate_rule.reader_entrypoints)
        if not lineage_entrypoints <= stage_entrypoints:
            raise ValueError(
                "every lineage writer and reader must belong to a required process stage"
            )

        extraction = provenance.trusted_extraction
        if extraction.artifact_path != self.measurement.artifact_path:
            raise ValueError("trusted provenance extraction must bind the measurement artifact")
        if extraction.extractor_id != self.measurement.extractor_id:
            raise ValueError("trusted provenance extraction must use the measurement extractor")
        if extraction.extractor_id != self.positive_control.extractor_id:
            raise ValueError("measurement and control must share the trusted provenance extractor")
        return self


class InitialManifest(_StrictModel):
    schema_version: Literal[1]
    task_id: str
    hash_algorithm: Literal["sha256"]
    directories: tuple[str, ...]
    files: dict[str, str]

    @field_validator("directories")
    @classmethod
    def _validate_directories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_relative_path(path) for path in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("initial manifest directory paths must be unique")
        if tuple(sorted(normalized)) != normalized:
            raise ValueError("initial manifest directories must be sorted")
        return normalized

    @field_validator("files")
    @classmethod
    def _validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("initial manifest must contain files")
        normalized: dict[str, str] = {}
        for path, digest in value.items():
            safe = _relative_path(path)
            if safe in normalized:
                raise ValueError(f"duplicate normalized manifest path {safe!r}")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"invalid SHA-256 for {path!r}")
            normalized[safe] = digest
        return normalized


class VariantOracle(_StrictModel):
    command: tuple[str, ...]
    expected_metrics: dict[str, float]
    expected_control_passed: StrictBool
    expected_ungated_scientific_status: ScientificStatus = Field(alias="expected_scientific_status")

    @field_validator("command")
    @classmethod
    def _command_not_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("variant command must not be empty")
        return value

    @field_validator("expected_metrics", mode="before")
    @classmethod
    def _strict_expected_metrics(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("expected_metrics must be an object")
        for key, metric in value.items():
            if not isinstance(key, str):
                raise ValueError("expected metric names must be strings")
            _finite_json_number(metric, field_name=f"expected metric {key!r}")
        return value

    @field_validator("expected_control_passed", mode="before")
    @classmethod
    def _strict_control_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("expected_control_passed must be a JSON boolean")
        return value


class StrategyOracle(_StrictModel):
    fixture_variant: str | None = None
    evidence_status: EvidenceStatus
    scientific_status: ScientificStatus
    reason_code: str
    description: str


class TaskOracle(_StrictModel):
    task_id: str
    variants: dict[str, VariantOracle]
    strategies: dict[str, StrategyOracle]


class OracleFile(_StrictModel):
    schema_version: Literal[1]
    tasks: dict[str, TaskOracle]


class PilotSuiteManifest(_StrictModel):
    schema_version: Literal[1]
    suite_id: str
    development_only: Literal[True]
    confirmatory_excluded: Literal[True]
    task_ids: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    pinned_files: dict[str, str]

    @model_validator(mode="after")
    def _unique_ids(self) -> PilotSuiteManifest:
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("suite contains duplicate task IDs")
        if len(set(self.strategy_ids)) != len(self.strategy_ids):
            raise ValueError("suite contains duplicate strategy IDs")
        for path, digest in self.pinned_files.items():
            _relative_path(path)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"invalid suite SHA-256 for {path!r}")
        return self


@dataclass(frozen=True)
class ScientificCheck:
    metrics: dict[str, float]
    control_passed: bool
    ungated_scientific_status: ScientificStatus


@dataclass(frozen=True)
class FixtureExecution:
    task_id: str
    variant_id: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    check: ScientificCheck
    enforced_constraints: tuple[str, ...]
    unenforced_constraints: tuple[str, ...]


def _portable_manifest(root: Path) -> dict[str, str]:
    return {path.replace("\\", "/"): digest for path, digest in file_manifest(str(root)).items()}


def _portable_directories(root: Path) -> tuple[str, ...]:
    directories: list[str] = []
    for directory, _, _ in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if relative.parts:
            directories.append(PurePosixPath(*relative.parts).as_posix())
    return tuple(sorted(directories))


def _reject_symlinks_and_special_files(root: Path, *, task_id: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise PilotTaskError(f"{task_id} repo root must be a regular directory")
    for dirpath, directories, filenames in os.walk(root, followlinks=False):
        parent = Path(dirpath)
        for name in directories:
            path = parent / name
            if path.is_symlink():
                raise PilotTaskError(f"{task_id} initial workspace contains symlink {path}")
            if not path.is_dir():
                raise PilotTaskError(f"{task_id} initial workspace contains special path {path}")
        for name in filenames:
            path = parent / name
            if path.is_symlink():
                raise PilotTaskError(f"{task_id} initial workspace contains symlink {path}")
            if not path.is_file():
                raise PilotTaskError(f"{task_id} initial workspace contains special path {path}")


def _contained_regular_file(root: Path, relative: str, *, task_id: str) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise PilotTaskError(f"{task_id} workspace root must be a regular directory")
    candidate = root
    for part in PurePosixPath(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PilotTaskError(f"{task_id} output {relative} has symlink component {candidate}")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PilotTaskError(f"{task_id} cannot resolve output {relative}: {exc}") from exc
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise PilotTaskError(f"{task_id} output {relative} escapes the workspace")
    if not resolved.is_file():
        raise PilotTaskError(f"{task_id} output {relative} must be a regular file")
    return resolved


def _csv_value_matches(text: str, expected: object) -> bool:
    if isinstance(expected, bool):
        return text.strip().lower() == str(expected).lower()
    if isinstance(expected, int):
        try:
            return int(text) == expected
        except ValueError:
            return False
    if isinstance(expected, float):
        try:
            observed = float(text)
        except ValueError:
            return False
        return math.isfinite(observed) and math.isclose(
            observed, expected, rel_tol=0.0, abs_tol=1e-12
        )
    return text == str(expected)


def _validate_csv_projection(
    path: Path,
    output: RequiredOutput,
    payload: dict[str, object],
    *,
    task_id: str,
) -> None:
    assert output.csv_columns is not None
    assert output.csv_key_column is not None
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != output.csv_columns:
                raise PilotTaskError(
                    f"{task_id} CSV {output.path} columns {reader.fieldnames} "
                    f"do not equal {list(output.csv_columns)}"
                )
            indexed: dict[str, dict[str, str]] = {}
            for row_number, row in enumerate(reader, start=2):
                if None in row or any(value is None for value in row.values()):
                    raise PilotTaskError(
                        f"{task_id} CSV {output.path} row {row_number} is malformed"
                    )
                key = row[output.csv_key_column]
                if key in indexed:
                    raise PilotTaskError(f"{task_id} CSV {output.path} repeats key {key!r}")
                indexed[key] = {column: value for column, value in row.items() if value is not None}
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PilotTaskError(f"cannot validate CSV {path}: {exc}") from exc

    expected_keys = set(output.mirrors_result_fields)
    if set(indexed) != expected_keys:
        raise PilotTaskError(
            f"{task_id} CSV {output.path} keys {sorted(indexed)} "
            f"do not equal mirrored fields {sorted(expected_keys)}"
        )
    value_columns = [column for column in output.csv_columns if column != output.csv_key_column]
    if len(value_columns) != 1:
        raise PilotTaskError(f"{task_id} CSV {output.path} must have one value column")
    value_column = value_columns[0]
    for field_name in output.mirrors_result_fields:
        if field_name not in payload or not _csv_value_matches(
            indexed[field_name][value_column], payload[field_name]
        ):
            raise PilotTaskError(
                f"{task_id} CSV {output.path} does not mirror result field {field_name!r}"
            )


@dataclass(frozen=True)
class ControlledTask:
    root: Path
    contract: ControlledTaskContract
    initial_manifest: InitialManifest
    oracle: TaskOracle

    @property
    def task_id(self) -> str:
        return self.contract.task_id

    @property
    def repo_root(self) -> Path:
        return self.root / "repo"

    def verify_initial_manifest(self) -> None:
        """Fail if any evaluated-workspace byte differs from the frozen fixture."""
        _reject_symlinks_and_special_files(self.repo_root, task_id=self.task_id)
        actual = _portable_manifest(self.repo_root)
        expected = self.initial_manifest.files
        actual_directories = _portable_directories(self.repo_root)
        expected_directories = self.initial_manifest.directories
        if actual != expected or actual_directories != expected_directories:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(
                path for path in set(actual) & set(expected) if actual[path] != expected[path]
            )
            missing_directories = sorted(set(expected_directories) - set(actual_directories))
            extra_directories = sorted(set(actual_directories) - set(expected_directories))
            raise PilotTaskError(
                f"{self.task_id} initial manifest mismatch "
                f"(missing={missing}, extra={extra}, changed={changed}, "
                f"missing_directories={missing_directories}, "
                f"extra_directories={extra_directories})"
            )

    def materialize(self, destination: str | Path) -> Path:
        """Copy only agent-visible repository files into a clean workspace."""
        self.verify_initial_manifest()
        target = Path(destination)
        if target.exists():
            raise PilotTaskError(f"pilot workspace already exists: {target}")
        shutil.copytree(self.repo_root, target)
        return target

    def verify_post_run_integrity(self, workspace: str | Path) -> None:
        """Ensure execution did not alter frozen files or add files outside mutable roots."""
        root = Path(workspace)
        _reject_symlinks_and_special_files(root, task_id=self.task_id)
        actual_files = _portable_manifest(root)
        expected_files = self.initial_manifest.files
        changed = sorted(
            path
            for path in set(actual_files) & set(expected_files)
            if actual_files[path] != expected_files[path]
        )
        missing = sorted(set(expected_files) - set(actual_files))
        mutable_roots = {
            *(
                PurePosixPath(output.path).parent.as_posix()
                for output in self.contract.required_outputs
            ),
            *(path for repair in self.contract.allowed_repairs for path in repair.allowed_paths),
        }
        mutable_roots.discard(".")

        def is_mutable(path: str) -> bool:
            return any(path == base or path.startswith(f"{base}/") for base in mutable_roots)

        unexpected_files = sorted(
            path for path in set(actual_files) - set(expected_files) if not is_mutable(path)
        )
        actual_directories = set(_portable_directories(root))
        expected_directories = set(self.initial_manifest.directories)
        missing_directories = sorted(expected_directories - actual_directories)
        unexpected_directories = sorted(
            path for path in actual_directories - expected_directories if not is_mutable(path)
        )
        if changed or missing or unexpected_files or missing_directories or unexpected_directories:
            raise PilotTaskError(
                f"{self.task_id} execution changed the frozen workspace "
                f"(changed={changed}, missing={missing}, unexpected_files={unexpected_files}, "
                f"missing_directories={missing_directories}, "
                f"unexpected_directories={unexpected_directories})"
            )

    def extract_and_evaluate(self, workspace: str | Path) -> ScientificCheck:
        """Run the harness-owned JSON extractor and scientific acceptance rules."""
        root = Path(workspace)
        output_paths: dict[str, Path] = {}
        for output in self.contract.required_outputs:
            path = _contained_regular_file(root, output.path, task_id=self.task_id)
            if path.stat().st_size < output.min_bytes:
                raise PilotTaskError(
                    f"{self.task_id} output {output.path} is smaller than {output.min_bytes} bytes"
                )
            output_paths[output.path] = path

        artifact = output_paths[self.contract.measurement.artifact_path]
        try:
            payload = _decode_json(artifact.read_text(encoding="utf-8"), source=str(artifact))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise PilotTaskError(f"cannot extract {artifact}: {exc}") from exc
        if not isinstance(payload, dict):
            raise PilotTaskError(f"{artifact} must contain one JSON object")

        schema = self.contract.result_schema
        expected_fields = {
            "task_id",
            *schema.numeric_fields,
            *schema.integer_fields,
            *schema.boolean_fields,
            *schema.literal_fields,
        }
        if not schema.allow_additional_fields and set(payload) != expected_fields:
            raise PilotTaskError(
                f"{artifact} fields {sorted(payload)} do not equal {sorted(expected_fields)}"
            )
        if payload.get("task_id") != schema.task_id:
            raise PilotTaskError(
                f"{artifact} task_id={payload.get('task_id')!r}, expected {schema.task_id!r}"
            )

        metrics: dict[str, float] = {}
        for field_name in schema.numeric_fields:
            value = payload.get(field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PilotTaskError(f"{artifact} field {field_name!r} must be numeric")
            try:
                number = float(value)
            except (OverflowError, ValueError) as exc:
                raise PilotTaskError(
                    f"{artifact} field {field_name!r} is outside the supported numeric range"
                ) from exc
            if not math.isfinite(number):
                raise PilotTaskError(f"{artifact} field {field_name!r} must be finite")
            metrics[field_name] = number
        for field_name, expected_integer in schema.integer_fields.items():
            value = payload.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value != expected_integer:
                raise PilotTaskError(
                    f"{artifact} field {field_name!r}={value!r}, "
                    f"expected integer {expected_integer}"
                )
        for field_name in schema.boolean_fields:
            if not isinstance(payload.get(field_name), bool):
                raise PilotTaskError(f"{artifact} field {field_name!r} must be boolean")
        for field_name, expected_literal in schema.literal_fields.items():
            value = payload.get(field_name)
            if type(value) is not type(expected_literal) or value != expected_literal:
                raise PilotTaskError(
                    f"{artifact} field {field_name!r}={value!r}, "
                    f"expected literal {expected_literal!r}"
                )

        for output in self.contract.required_outputs:
            if output.media_type == "text/csv":
                _validate_csv_projection(
                    output_paths[output.path], output, payload, task_id=self.task_id
                )

        control = self.contract.positive_control
        control_passed = abs(metrics[control.metric] - control.expected) <= control.tolerance
        if control.reported_pass_field is not None:
            reported_control = payload[control.reported_pass_field]
            if reported_control is not control_passed:
                raise PilotTaskError(
                    f"{artifact} field {control.reported_pass_field!r}={reported_control!r} "
                    f"disagrees with trusted control result {control_passed}"
                )
        if not control_passed:
            status: ScientificStatus = "UNDETERMINED"
        else:
            rule = self.contract.measurement
            status = (
                "SUPPORTS"
                if _measurement_passes(
                    metrics[rule.metric], rule.operator, rule.threshold, rule.tolerance
                )
                else "DOES_NOT_SUPPORT"
            )
        return ScientificCheck(
            metrics=metrics,
            control_passed=control_passed,
            ungated_scientific_status=status,
        )


@dataclass(frozen=True)
class PilotSuite:
    root: Path
    manifest: PilotSuiteManifest
    tasks: tuple[ControlledTask, ...]

    def task(self, task_id: str) -> ControlledTask:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise KeyError(f"unknown pilot task {task_id!r}")


def _measurement_passes(
    observed: float,
    operator: Literal[">=", ">", "<=", "<"],
    threshold: float,
    tolerance: float,
) -> bool:
    if operator == ">=":
        return observed >= threshold - tolerance
    if operator == ">":
        return observed > threshold
    if operator == "<=":
        return observed <= threshold + tolerance
    return observed < threshold


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _decode_json(text: str, *, source: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON in {source}: {exc}") from exc


def _read_json(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
        return _decode_json(text, source=str(path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PilotTaskError(f"cannot read {path}: {exc}") from exc


def _validate_task(task: ControlledTask, strategy_ids: set[str]) -> None:
    if task.initial_manifest.task_id != task.task_id or task.oracle.task_id != task.task_id:
        raise PilotTaskError(f"task ID disagreement under {task.root}")
    if task.contract.result_schema.task_id != task.task_id:
        raise PilotTaskError(f"result schema ID disagrees for {task.task_id}")
    if set(task.oracle.strategies) != strategy_ids:
        raise PilotTaskError(
            f"{task.task_id} strategy IDs {sorted(task.oracle.strategies)} "
            f"do not equal suite IDs {sorted(strategy_ids)}"
        )

    if set(task.oracle.variants) != set(_EXPECTED_VARIANTS.values()):
        raise PilotTaskError(
            f"{task.task_id} fixture variants do not equal the frozen strategy variants"
        )
    expected_numeric_fields = set(task.contract.result_schema.numeric_fields)
    for variant_id, variant in task.oracle.variants.items():
        if set(variant.expected_metrics) != expected_numeric_fields:
            raise PilotTaskError(
                f"{task.task_id}/{variant_id} expected metric keys "
                f"{sorted(variant.expected_metrics)} do not equal "
                f"{sorted(expected_numeric_fields)}"
            )
        control = task.contract.positive_control
        control_passed = (
            abs(variant.expected_metrics[control.metric] - control.expected) <= control.tolerance
        )
        if control_passed is not variant.expected_control_passed:
            raise PilotTaskError(
                f"{task.task_id}/{variant_id} oracle control label disagrees with its metrics"
            )
        if not control_passed:
            ungated_status: ScientificStatus = "UNDETERMINED"
        else:
            measurement = task.contract.measurement
            ungated_status = (
                "SUPPORTS"
                if _measurement_passes(
                    variant.expected_metrics[measurement.metric],
                    measurement.operator,
                    measurement.threshold,
                    measurement.tolerance,
                )
                else "DOES_NOT_SUPPORT"
            )
        if ungated_status != variant.expected_ungated_scientific_status:
            raise PilotTaskError(
                f"{task.task_id}/{variant_id} ungated status disagrees with its metrics"
            )

    expected_i2: tuple[EvidenceStatus, ScientificStatus, str]
    if task.task_id == "pilot_weighted_mean":
        expected_i2 = ("INVALID", "UNDETERMINED", "forbidden_answer_source")
    elif task.task_id == "pilot_seeded_comparison":
        expected_i2 = ("INSUFFICIENT", "UNDETERMINED", "missing_derivation_witness")
    else:
        raise PilotTaskError(f"unexpected pilot task ID {task.task_id}")
    expected_profiles = {**_COMMON_STRATEGY_PROFILE, "I2": expected_i2}
    for strategy_id, expected_variant in _EXPECTED_VARIANTS.items():
        strategy = task.oracle.strategies[strategy_id]
        actual_profile = (
            strategy.evidence_status,
            strategy.scientific_status,
            strategy.reason_code,
        )
        if strategy.fixture_variant != expected_variant:
            raise PilotTaskError(
                f"{task.task_id}/{strategy_id} maps to {strategy.fixture_variant!r}, "
                f"expected {expected_variant!r}"
            )
        if actual_profile != expected_profiles[strategy_id]:
            raise PilotTaskError(
                f"{task.task_id}/{strategy_id} profile {actual_profile!r}, "
                f"expected {expected_profiles[strategy_id]!r}"
            )

    if task.oracle.variants["primary"].command != task.contract.required_command:
        raise PilotTaskError(f"{task.task_id} V1 command does not equal required_command")
    if len(task.contract.allowed_repairs) != 1:
        raise PilotTaskError(f"{task.task_id} must declare exactly one pilot repair")
    repair = task.contract.allowed_repairs[0]
    if task.oracle.variants["environment_repair"].command != repair.command:
        raise PilotTaskError(f"{task.task_id} V2 command is not the allowlisted repair")
    for path in repair.allowed_paths:
        if path in task.initial_manifest.files or path in task.initial_manifest.directories:
            raise PilotTaskError(
                f"{task.task_id} repair target {path!r} exists in the initial workspace"
            )
    if len(task.contract.allowed_scientific_variants) != 1:
        raise PilotTaskError(f"{task.task_id} must declare exactly one scientific variant")
    authorized = task.contract.allowed_scientific_variants[0]
    if task.oracle.variants["authorized_alternative"].command != authorized.command:
        raise PilotTaskError(f"{task.task_id} V3 command is not the allowlisted variant")
    if task.oracle.variants["negative_science"].command != task.contract.condition_commands.get(
        "negative_science"
    ):
        raise PilotTaskError(f"{task.task_id} V4 command is not the declared negative condition")
    if task.oracle.variants["failed_control"].command != task.contract.condition_commands.get(
        "failed_control"
    ):
        raise PilotTaskError(f"{task.task_id} I6 command is not the declared failed control")

    pinned = [*task.contract.declared_inputs, *task.contract.forbidden_ancestors]
    for condition in task.contract.condition_inputs.values():
        pinned.extend(condition)
    for item in pinned:
        digest = task.initial_manifest.files.get(item.path)
        if digest != item.sha256:
            raise PilotTaskError(
                f"{task.task_id} pin for {item.path} is {item.sha256}, manifest has {digest}"
            )
    for output in task.contract.required_outputs:
        if output.path in task.initial_manifest.files:
            raise PilotTaskError(
                f"{task.task_id} required output {output.path} exists in the initial workspace"
            )
    for stage in task.contract.provenance.process_stages:
        for entrypoint in stage.command_entrypoints:
            if entrypoint not in task.initial_manifest.files:
                raise PilotTaskError(
                    f"{task.task_id} provenance stage entrypoint {entrypoint!r} "
                    "is not pinned in the initial workspace"
                )
    for intermediate in task.contract.provenance.intermediate_artifacts:
        if intermediate.path in task.initial_manifest.files:
            raise PilotTaskError(
                f"{task.task_id} provenance intermediate {intermediate.path!r} "
                "exists in the initial workspace"
            )
    for strategy_id, strategy in task.oracle.strategies.items():
        if (
            strategy.fixture_variant is not None
            and strategy.fixture_variant not in task.oracle.variants
        ):
            raise PilotTaskError(
                f"{task.task_id} strategy {strategy_id} names unknown fixture variant "
                f"{strategy.fixture_variant}"
            )
    task.verify_initial_manifest()


def load_pilot_suite(root: str | Path | None = None) -> PilotSuite:
    """Load and integrity-check the two development-only provenance tasks."""
    suite_root = Path(root) if root is not None else DEFAULT_PILOT_ROOT
    manifest = PilotSuiteManifest.model_validate(_read_json(suite_root / "suite.json"))
    if manifest.task_ids != _EXPECTED_TASK_IDS:
        raise PilotTaskError(
            f"pilot task IDs {manifest.task_ids!r} do not equal {_EXPECTED_TASK_IDS!r}"
        )
    if manifest.strategy_ids != tuple(_EXPECTED_VARIANTS):
        raise PilotTaskError("pilot strategy IDs do not equal the frozen V1--I6 profile")
    expected_pins = {
        "trusted/oracles.json",
        *(
            f"tasks/{task_id}/{filename}"
            for task_id in manifest.task_ids
            for filename in ("contract.json", "initial_manifest.json")
        ),
    }
    if set(manifest.pinned_files) != expected_pins:
        raise PilotTaskError(
            f"suite pin paths {sorted(manifest.pinned_files)} do not equal {sorted(expected_pins)}"
        )
    for relative, expected_digest in manifest.pinned_files.items():
        target = suite_root / relative
        try:
            actual_digest = sha256_file(str(target))
        except OSError as exc:
            raise PilotTaskError(f"cannot hash suite file {target}: {exc}") from exc
        if actual_digest != expected_digest:
            raise PilotTaskError(
                f"suite file {relative} has SHA-256 {actual_digest}, expected {expected_digest}"
            )
    oracle_file = OracleFile.model_validate(_read_json(suite_root / "trusted" / "oracles.json"))
    if set(oracle_file.tasks) != set(manifest.task_ids):
        raise PilotTaskError("oracle task IDs do not exactly match suite task IDs")

    tasks: list[ControlledTask] = []
    strategy_ids = set(manifest.strategy_ids)
    for task_id in manifest.task_ids:
        task_root = suite_root / "tasks" / task_id
        contract = ControlledTaskContract.model_validate(_read_json(task_root / "contract.json"))
        initial = InitialManifest.model_validate(_read_json(task_root / "initial_manifest.json"))
        task = ControlledTask(
            root=task_root,
            contract=contract,
            initial_manifest=initial,
            oracle=oracle_file.tasks[task_id],
        )
        if task.task_id != task_id:
            raise PilotTaskError(f"suite task {task_id!r} loaded contract for {task.task_id!r}")
        _validate_task(task, strategy_ids)
        tasks.append(task)
    return PilotSuite(root=suite_root, manifest=manifest, tasks=tuple(tasks))


def _python_requirement_satisfied(requirement: str) -> bool:
    required = tuple(int(part) for part in requirement[2:].split("."))
    running = sys.version_info[: len(required)]
    return running >= required


def _fixture_environment() -> dict[str, str]:
    search_path = [str(Path(sys.executable).resolve().parent), *os.defpath.split(os.pathsep)]
    environment = {
        "LC_ALL": "C",
        "PATH": os.pathsep.join(dict.fromkeys(search_path)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    for name in ("SYSTEMROOT", "TMPDIR", "TMP", "TEMP"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


def run_fixture_variant(
    task: ControlledTask,
    variant_id: str,
    workspace: str | Path,
) -> FixtureExecution:
    """Execute one trusted fixture variant without making a provenance claim.

    This construction self-check enforces the Python requirement, a sanitized
    environment, and a process-tree timeout. Network isolation and the Linux
    monitor platform remain requirements for the later monitored pilot run.
    """
    try:
        variant = task.oracle.variants[variant_id]
    except KeyError as exc:
        raise PilotTaskError(f"{task.task_id} has no fixture variant {variant_id!r}") from exc
    if not _python_requirement_satisfied(task.contract.runtime.python):
        raise PilotTaskError(
            f"{task.task_id} requires Python {task.contract.runtime.python}; "
            f"running {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
    root = task.materialize(workspace)
    command = tuple(sys.executable if token == "{python}" else token for token in variant.command)
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=_fixture_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise PilotTaskError(f"cannot execute {task.task_id}/{variant_id}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=task.contract.runtime.timeout_s)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate()
        raise PilotTaskError(
            f"{task.task_id}/{variant_id} exceeded {task.contract.runtime.timeout_s}s "
            "process-tree timeout"
        ) from exc
    returncode = process.returncode
    if returncode is None:
        raise PilotTaskError(f"{task.task_id}/{variant_id} did not report a return code")
    if returncode != 0:
        raise PilotTaskError(f"{task.task_id}/{variant_id} exited {returncode}: {stderr.strip()}")
    task.verify_post_run_integrity(root)
    check = task.extract_and_evaluate(root)
    for metric, expected in variant.expected_metrics.items():
        observed = check.metrics.get(metric)
        if observed is None or not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-12):
            raise PilotTaskError(
                f"{task.task_id}/{variant_id} metric {metric}={observed}, expected {expected}"
            )
    if check.control_passed is not variant.expected_control_passed:
        raise PilotTaskError(
            f"{task.task_id}/{variant_id} control={check.control_passed}, "
            f"expected {variant.expected_control_passed}"
        )
    if check.ungated_scientific_status != variant.expected_ungated_scientific_status:
        raise PilotTaskError(
            f"{task.task_id}/{variant_id} ungated scientific status="
            f"{check.ungated_scientific_status}, "
            f"expected {variant.expected_ungated_scientific_status}"
        )
    return FixtureExecution(
        task_id=task.task_id,
        variant_id=variant_id,
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        check=check,
        enforced_constraints=(
            "python_requirement",
            "sanitized_environment",
            "process_tree_timeout" if os.name == "posix" else "top_level_timeout",
        ),
        unenforced_constraints=(
            "network_isolation",
            "linux_monitor_platform",
            *(("descendant_process_timeout",) if os.name != "posix" else ()),
        ),
    )


__all__ = [
    "ControlledTask",
    "ControlledTaskContract",
    "DEFAULT_PILOT_ROOT",
    "FixtureExecution",
    "PilotSuite",
    "PilotSuiteManifest",
    "PilotTaskError",
    "ScientificCheck",
    "load_pilot_suite",
    "run_fixture_variant",
]
