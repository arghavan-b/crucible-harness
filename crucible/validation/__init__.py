"""Plan validation gates (design §4.2)."""

from __future__ import annotations

from crucible.schemas import Severity, ValidationFinding, ValidationRecord, Waiver

from .gates import (
    DEFAULT_INITIAL_FACTS,
    DEFAULT_NETWORK_ALLOWLIST,
    SEVERITY_BY_GATE,
    PlanValidationError,
    Violation,
    validate,
    validate_or_raise,
    validate_plan,
)
from .predicates import KNOWN_PREDICATES, Predicate, parse_predicate

__all__ = [
    "DEFAULT_INITIAL_FACTS",
    "DEFAULT_NETWORK_ALLOWLIST",
    "KNOWN_PREDICATES",
    "SEVERITY_BY_GATE",
    "PlanValidationError",
    "Predicate",
    "Severity",
    "ValidationFinding",
    "ValidationRecord",
    "Violation",
    "Waiver",
    "parse_predicate",
    "validate",
    "validate_or_raise",
    "validate_plan",
]
