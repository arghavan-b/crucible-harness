"""Verdict — the product (design §4.3).

The system's output is not the execution; it is the verdict and its evidence
chain. A false SUCCESS or false RESULT_NEGATIVE is the cardinal failure.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import VerdictStatus


class PositiveControlEvidence(BaseModel):
    status: str  # "passed" | "failed"
    observed: float | None = None


class ExecutionIntegrity(BaseModel):
    all_steps_verified: bool = False
    repairs_applied: list[str] = Field(default_factory=list)
    repair_risk_note: str | None = None


class ResultEvidence(BaseModel):
    observed: dict[str, list[float]] = Field(default_factory=dict)
    test: str | None = Field(default=None, description="e.g. 'one-sided Welch t, p=0.21'")
    conclusion: str | None = None


class Evidence(BaseModel):
    positive_control: PositiveControlEvidence | None = None
    execution_integrity: ExecutionIntegrity = Field(default_factory=ExecutionIntegrity)
    result: ResultEvidence | None = None
    caveats: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    trace_id: str
    container_digest: str | None = None
    replay_command: str | None = None


class Verdict(BaseModel):
    experiment_id: str
    claim_id: str
    status: VerdictStatus
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    reason: str | None = Field(
        default=None, description="Required for EXECUTION_FAILURE / INCONCLUSIVE."
    )
    evidence: Evidence = Field(default_factory=Evidence)
    provenance: Provenance | None = None
