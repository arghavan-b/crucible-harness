"""Nondeterminism policy (design §4.4).

Real experiments diverge for legitimate reasons: log timestamps, unseeded RNG,
GPU float nondeterminism. A reproducibility certificate therefore carries a
policy declaring which divergences are *expected*. Replay uses this policy to
distinguish tolerable nondeterminism from a real reproduction failure.

The policy is data (serialized into the certificate); the engine that interprets
it lives in crucible.certificate.policy.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .enums import ToleranceType


class RuleMode(str, Enum):
    EXEMPT = "exempt"              # artifact may diverge freely (e.g. logs)
    NUMERIC_JSON = "numeric_json"  # compare parsed JSON numbers within tolerance
    NORMALIZE = "normalize"        # strip a volatile pattern, then compare exactly


class ArtifactRule(BaseModel):
    pattern: str = Field(..., description="glob matched against the artifact's relative path")
    mode: RuleMode
    tolerance: float = Field(0.0, description="NUMERIC_JSON: allowed |delta| per numeric leaf")
    tolerance_type: ToleranceType = ToleranceType.ABSOLUTE
    strip_pattern: str | None = Field(
        default=None,
        description="NORMALIZE: regex whose matches are removed from both sides before comparison",
    )
    note: str | None = Field(default=None, description="Why this divergence is expected.")


class NondeterminismPolicy(BaseModel):
    """Ordered rules; the first whose glob matches an artifact wins. An empty
    policy is strict — any byte divergence is unexpected."""

    rules: list[ArtifactRule] = Field(default_factory=list)
