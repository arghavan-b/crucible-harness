"""Core Crucible schemas — the API surface (design §4).

The schemas ARE the API. Everything else (executor, verifiers, adjudicator,
CLI) is built against these typed objects.
"""

from __future__ import annotations

from .certificate import PinnedInputs, ReproducibilityCertificate
from .enums import (
    FailureCategory,
    HypothesisType,
    PathClass,
    PlaybookStatus,
    RollbackKind,
    StepState,
    ToleranceType,
    VerdictStatus,
)
from .ontology import ONTOLOGY_VERSION, StepType
from .plan import Action, ExecutionPlan, Rollback, Step, StepBudget
from .policy import ArtifactRule, NondeterminismPolicy, RuleMode
from .provenance import (
    PROVENANCE_PREDICATES,
    EvidenceStatus,
    FileVersionWitness,
    PredicateEvaluation,
    PredicateStatus,
    ProvenanceGateDecision,
    ProvenancePredicate,
    ScientificStatus,
)
from .spec import (
    Budget,
    ClaimUnderTest,
    EnvironmentConstraints,
    ExperimentSpec,
    GpuConstraint,
    Hypothesis,
    PositiveControl,
    ScalePolicy,
    Source,
    Tolerance,
)
from .validation import (
    Severity,
    ValidationFinding,
    ValidationRecord,
    Waiver,
)
from .verdict import (
    Evidence,
    ExecutionIntegrity,
    PositiveControlEvidence,
    Provenance,
    ResultEvidence,
    Verdict,
)

__all__ = [
    "ONTOLOGY_VERSION",
    "Action",
    "ArtifactRule",
    "Budget",
    "ClaimUnderTest",
    "EnvironmentConstraints",
    "Evidence",
    "EvidenceStatus",
    "ExecutionIntegrity",
    "ExecutionPlan",
    "ExperimentSpec",
    "FailureCategory",
    "FileVersionWitness",
    "GpuConstraint",
    "Hypothesis",
    "HypothesisType",
    "NondeterminismPolicy",
    "PathClass",
    "PinnedInputs",
    "PlaybookStatus",
    "PositiveControl",
    "PositiveControlEvidence",
    "PredicateEvaluation",
    "PredicateStatus",
    "Provenance",
    "ProvenanceGateDecision",
    "ProvenancePredicate",
    "PROVENANCE_PREDICATES",
    "ReproducibilityCertificate",
    "ResultEvidence",
    "Rollback",
    "RollbackKind",
    "RuleMode",
    "ScalePolicy",
    "Severity",
    "ScientificStatus",
    "Source",
    "Step",
    "StepBudget",
    "StepState",
    "StepType",
    "Tolerance",
    "ToleranceType",
    "ValidationFinding",
    "ValidationRecord",
    "Verdict",
    "VerdictStatus",
    "Waiver",
]
