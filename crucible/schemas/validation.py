"""Validation severity, waivers, and the recorded result (design §4.2).

Not every finding should block execution. Findings carry a severity; only
unwaived ERRORs stop a run. A waiver lets an experiment override a specific
finding with a recorded justification, and the whole ValidationRecord is written
into the trace and reproducibility certificate so a third party can see exactly
what was checked, what failed, and what was waived (and why).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "error"        # blocks execution unless waived
    WARNING = "warning"    # recorded, does not block
    ADVISORY = "advisory"  # informational


class Waiver(BaseModel):
    """Suppresses matching findings, with an auditable justification."""

    gate: str = Field(..., description="gate name to waive, e.g. 'network_allowlist'")
    step_id: str | None = Field(default=None, description="limit to one step, or all if null")
    contains: str | None = Field(
        default=None, description="only waive if the finding message contains this substring"
    )
    reason: str = Field(..., description="why this finding is acceptable for this experiment")
    author: str | None = None

    def matches(self, gate: str, message: str, step_id: str | None) -> bool:
        if self.gate != gate:
            return False
        if self.step_id is not None and self.step_id != step_id:
            return False
        if self.contains is not None and self.contains not in message:
            return False
        return True


class ValidationFinding(BaseModel):
    gate: str
    message: str
    severity: Severity
    step_id: str | None = None
    waived: bool = False
    waiver_reason: str | None = None


class ValidationRecord(BaseModel):
    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)

    def blocking(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity is Severity.ERROR and not f.waived]

    def summary(self) -> str:
        errors = sum(1 for f in self.findings if f.severity is Severity.ERROR and not f.waived)
        warnings = sum(1 for f in self.findings if f.severity is Severity.WARNING and not f.waived)
        waived = sum(1 for f in self.findings if f.waived)
        verdict = "PASSED" if self.passed else "FAILED"
        return f"{verdict} — {errors} error(s), {warnings} warning(s), {waived} waived"
