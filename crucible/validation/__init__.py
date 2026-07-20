"""Plan validation gates (design §4.2)."""

from __future__ import annotations

from .gates import (
    DEFAULT_INITIAL_FACTS,
    DEFAULT_NETWORK_ALLOWLIST,
    PlanValidationError,
    Violation,
    validate_or_raise,
    validate_plan,
)
from .predicates import KNOWN_PREDICATES, Predicate, parse_predicate

__all__ = [
    "DEFAULT_INITIAL_FACTS",
    "DEFAULT_NETWORK_ALLOWLIST",
    "KNOWN_PREDICATES",
    "PlanValidationError",
    "Predicate",
    "Violation",
    "parse_predicate",
    "validate_or_raise",
    "validate_plan",
]
