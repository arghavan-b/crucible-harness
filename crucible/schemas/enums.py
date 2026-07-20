"""Closed enumerations shared across Crucible schemas.

Keeping these closed is deliberate: the plan language and verdict space are
small and fixed so the harness can validate them (design §4.2, §4.3).
"""

from __future__ import annotations

from enum import Enum


class VerdictStatus(str, Enum):
    """The four terminal verdicts (design §4.3, §8.1).

    SUCCESS and RESULT_NEGATIVE may only be emitted when the positive control
    passed, every gating verifier passed, and no unresolved INCONCLUSIVE
    condition exists. INCONCLUSIVE is the default under uncertainty.
    """

    SUCCESS = "SUCCESS"
    RESULT_NEGATIVE = "RESULT_NEGATIVE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    INCONCLUSIVE = "INCONCLUSIVE"


class HypothesisType(str, Enum):
    COMPARATIVE = "comparative"
    REPRODUCTION = "reproduction"
    ABLATION = "ablation"
    EXPLORATORY = "exploratory"


class ToleranceType(str, Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class PathClass(str, Enum):
    """Infrastructure vs scientific path (design §8.2).

    Repairs are auto-applied only on the infrastructure path. Anything unknown
    defaults to SCIENTIFIC (conservative).
    """

    INFRASTRUCTURE = "infrastructure"
    SCIENTIFIC = "scientific"


class StepState(str, Enum):
    """Transactional executor state machine (design §6.4, §16 of RAPP)."""

    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DIAGNOSING = "DIAGNOSING"   # Stage 1+
    RECOVERING = "RECOVERING"   # Stage 1+
    ROLLING_BACK = "ROLLING_BACK"
    ESCALATED = "ESCALATED"


class RollbackKind(str, Enum):
    SNAPSHOT_RESTORE = "snapshot_restore"
    COMPENSATING_ACTION = "compensating_action"
    UNSUPPORTED = "unsupported"


class PlaybookStatus(str, Enum):
    """Playbook promotion lifecycle (design §9.3). Stage 0 authors trusted
    playbooks directly; the promotion pipeline is Stage 1."""

    CANDIDATE = "candidate"
    VALIDATED = "validated"
    TRUSTED = "trusted"


class FailureCategory(str, Enum):
    """Top level of the research-code failure taxonomy (design §9.1)."""

    ENVIRONMENT = "environment"
    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    CONFIGURATION = "configuration"
    INPUT = "input"
    IMPLEMENTATION = "implementation"
