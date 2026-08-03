"""Typed claims, acceptance policies, and the Procedure Compiler.

paper/report and/or repo -> Claim(s) + AcceptancePolicy + ArtifactReport
(domain design §2–§3). This is the T0 tier: static, no execution, no network.
"""

from __future__ import annotations

from .adapter import dropped_requirements, spec_from_claim, specs_from_claims
from .compiler import (
    ArtifactFinding,
    ArtifactKind,
    ArtifactLocation,
    ArtifactReport,
    Availability,
    compile_procedure,
    repo_summary,
)
from .extract import EXTRACTION_INSTRUCTIONS, Extractor, HeuristicExtractor, LLMExtractor
from .intake import ClaimIntake, ClaimIntakeResult
from .policy import default_policy, describe, ensure_policies, ensure_policy, policy_for_claim
from .runconfig import ConfigFile, RunCommand, RunConfig, extract_run_config
from .schema import (
    AcceptancePolicy,
    AssayType,
    Claim,
    ClaimContext,
    ClaimSet,
    ClaimType,
    DatasetRef,
    DedupPolicy,
    EvidenceRequirement,
    Margin,
    PolicySource,
    Relation,
    ReportedValues,
    Representation,
    SourceRef,
    SplitMethod,
    SplitSpec,
    Statement,
)

__all__ = [
    "EXTRACTION_INSTRUCTIONS",
    "AcceptancePolicy",
    "ArtifactFinding",
    "ArtifactKind",
    "ArtifactLocation",
    "ArtifactReport",
    "AssayType",
    "Availability",
    "Claim",
    "ClaimContext",
    "ClaimIntake",
    "ClaimIntakeResult",
    "ClaimSet",
    "ClaimType",
    "ConfigFile",
    "DatasetRef",
    "DedupPolicy",
    "EvidenceRequirement",
    "Extractor",
    "HeuristicExtractor",
    "LLMExtractor",
    "Margin",
    "PolicySource",
    "Relation",
    "ReportedValues",
    "Representation",
    "RunCommand",
    "RunConfig",
    "SourceRef",
    "SplitMethod",
    "SplitSpec",
    "Statement",
    "compile_procedure",
    "default_policy",
    "describe",
    "dropped_requirements",
    "ensure_policies",
    "ensure_policy",
    "extract_run_config",
    "policy_for_claim",
    "repo_summary",
    "spec_from_claim",
    "specs_from_claims",
]
