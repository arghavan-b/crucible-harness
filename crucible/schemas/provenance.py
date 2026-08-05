"""Typed decisions emitted by the deterministic causal-provenance gate."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


EvidenceStatus = Literal["ADMISSIBLE", "INSUFFICIENT", "INVALID", "EXECUTION_FAILURE"]
ScientificStatus = Literal["SUPPORTS", "DOES_NOT_SUPPORT", "UNDETERMINED"]
PredicateStatus = Literal["SATISFIED", "VIOLATED", "UNSUPPORTED"]
ProvenancePredicate = Literal[
    "executed",
    "read_declared_input",
    "fresh",
    "written_by",
    "derived_from",
    "not_derived_from",
    "metric_extracted_by",
    "control_passed",
    "within_budget",
    "repair_allowed",
    "scientific_files_unchanged",
]

PROVENANCE_PREDICATES: tuple[ProvenancePredicate, ...] = (
    "executed",
    "read_declared_input",
    "fresh",
    "written_by",
    "derived_from",
    "not_derived_from",
    "metric_extracted_by",
    "control_passed",
    "within_budget",
    "repair_allowed",
    "scientific_files_unchanged",
)


class _StrictProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PredicateEvaluation(_StrictProvenanceModel):
    predicate: ProvenancePredicate
    status: PredicateStatus
    reason_code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    capture_ids: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    pids: tuple[StrictInt, ...] = ()

    @model_validator(mode="after")
    def _witnesses_are_unique(self) -> PredicateEvaluation:
        if len(set(self.capture_ids)) != len(self.capture_ids):
            raise ValueError("predicate capture witnesses must be unique")
        if len(set(self.paths)) != len(self.paths):
            raise ValueError("predicate path witnesses must be unique")
        if len(set(self.pids)) != len(self.pids) or any(pid <= 0 for pid in self.pids):
            raise ValueError("predicate PID witnesses must be positive and unique")
        return self


class FileVersionWitness(_StrictProvenanceModel):
    path: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    capture_id: str = Field(min_length=1)
    writer_pid: StrictInt = Field(gt=0)
    writer_entrypoints: tuple[str, ...]
    ancestors: tuple[str, ...]
    write_sequences: tuple[StrictInt, ...]
    final_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _version_witness_is_consistent(self) -> FileVersionWitness:
        if not self.write_sequences:
            raise ValueError("file-version witnesses require write events")
        if tuple(sorted(set(self.write_sequences))) != self.write_sequences:
            raise ValueError("file-version write sequences must be unique and sorted")
        if tuple(sorted(set(self.writer_entrypoints))) != self.writer_entrypoints:
            raise ValueError("writer entrypoints must be unique and sorted")
        if tuple(sorted(set(self.ancestors))) != self.ancestors:
            raise ValueError("file-version ancestors must be unique and sorted")
        return self


class ProvenanceGateDecision(_StrictProvenanceModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1)
    contract_schema_version: StrictInt = Field(gt=0)
    trace_id: str = Field(min_length=1)
    evidence_status: EvidenceStatus
    scientific_status: ScientificStatus
    reason_code: str = Field(min_length=1)
    input_profile: str | None = None
    predicates: tuple[PredicateEvaluation, ...]
    final_versions: tuple[FileVersionWitness, ...] = ()
    metrics: dict[str, float] = Field(default_factory=dict)
    control_passed: StrictBool | None = None

    @model_validator(mode="after")
    def _decision_is_consistent(self) -> ProvenanceGateDecision:
        names = tuple(item.predicate for item in self.predicates)
        if names != PROVENANCE_PREDICATES:
            raise ValueError("gate decisions must evaluate every frozen predicate in order")
        statuses = {item.status for item in self.predicates}
        if self.evidence_status == "ADMISSIBLE":
            if statuses != {"SATISFIED"}:
                raise ValueError("admissible decisions require every predicate to be satisfied")
            if self.control_passed is not True:
                raise ValueError("admissible decisions require a passing control")
        else:
            if self.scientific_status != "UNDETERMINED":
                raise ValueError("non-admissible evidence requires undetermined science")
            if self.evidence_status == "INVALID" and "VIOLATED" not in statuses:
                raise ValueError("invalid decisions require a violated predicate")
            if self.evidence_status == "INSUFFICIENT" and (
                "UNSUPPORTED" not in statuses or "VIOLATED" in statuses
            ):
                raise ValueError(
                    "insufficient decisions require unsupported but no violated predicates"
                )
        return self


__all__ = [
    "PROVENANCE_PREDICATES",
    "EvidenceStatus",
    "FileVersionWitness",
    "PredicateEvaluation",
    "PredicateStatus",
    "ProvenanceGateDecision",
    "ProvenancePredicate",
    "ScientificStatus",
]
