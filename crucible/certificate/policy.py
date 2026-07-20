"""Nondeterminism classification engine (design §4.4).

Given a policy (data, from the certificate) and an artifact that diverged between
the original run and a replay, decide whether the divergence is EXPECTED (a
declared, tolerable nondeterminism source) or UNEXPECTED (a real reproduction
failure). Only UNEXPECTED divergence breaks reproduction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatch

from crucible.schemas import ToleranceType
from crucible.schemas.policy import ArtifactRule, NondeterminismPolicy, RuleMode


class Classification(str, Enum):
    MATCHED = "matched"                        # byte identical
    EXPECTED = "expected_divergence"           # diverged, but policy-allowed
    UNEXPECTED = "unexpected_divergence"       # diverged, real nondeterminism
    MISSING = "missing"                        # expected artifact not produced
    UNEXPECTED_ARTIFACT = "unexpected_artifact"  # produced but not certified


@dataclass
class ArtifactJudgement:
    path: str
    classification: Classification
    detail: str | None = None


def match_rule(policy: NondeterminismPolicy, path: str) -> ArtifactRule | None:
    for rule in policy.rules:
        if fnmatch(path, rule.pattern):
            return rule
    return None


def default_policy() -> NondeterminismPolicy:
    """Sensible defaults: common volatile files may diverge freely."""
    return NondeterminismPolicy(
        rules=[
            ArtifactRule(pattern="*.log", mode=RuleMode.EXEMPT, note="log output is volatile"),
            ArtifactRule(pattern="logs/*", mode=RuleMode.EXEMPT, note="log directory"),
            ArtifactRule(pattern="*.tmp", mode=RuleMode.EXEMPT, note="temp files"),
            ArtifactRule(pattern="**/__pycache__/*", mode=RuleMode.EXEMPT, note="bytecode cache"),
        ]
    )


def _within_tolerance(a: float, b: float, tol: float, kind: ToleranceType) -> bool:
    if kind is ToleranceType.RELATIVE:
        denom = max(abs(a), 1e-12)
        return abs(a - b) / denom <= tol
    return abs(a - b) <= tol


def _numeric_json_equal(
    original: object, replayed: object, tol: float, kind: ToleranceType, path: str = "$"
) -> tuple[bool, str | None]:
    """Structural equality where numeric leaves may differ within tolerance."""
    if isinstance(original, bool) or isinstance(replayed, bool):
        if original != replayed:
            return False, f"{path}: {original!r} != {replayed!r}"
        return True, None
    if isinstance(original, (int, float)) and isinstance(replayed, (int, float)):
        if _within_tolerance(float(original), float(replayed), tol, kind):
            return True, None
        return False, f"{path}: {original} vs {replayed} exceeds tolerance {tol}"
    if isinstance(original, dict) and isinstance(replayed, dict):
        if original.keys() != replayed.keys():
            return False, f"{path}: keys differ"
        for k in original:
            ok, detail = _numeric_json_equal(original[k], replayed[k], tol, kind, f"{path}.{k}")
            if not ok:
                return False, detail
        return True, None
    if isinstance(original, list) and isinstance(replayed, list):
        if len(original) != len(replayed):
            return False, f"{path}: length {len(original)} != {len(replayed)}"
        for i, (a, b) in enumerate(zip(original, replayed)):
            ok, detail = _numeric_json_equal(a, b, tol, kind, f"{path}[{i}]")
            if not ok:
                return False, detail
        return True, None
    if original != replayed:
        return False, f"{path}: {original!r} != {replayed!r}"
    return True, None


def classify_divergence(
    policy: NondeterminismPolicy, path: str, original: str | None, replayed: str | None
) -> ArtifactJudgement:
    """Classify an artifact whose bytes differ between original and replay."""
    rule = match_rule(policy, path)
    if rule is None:
        return ArtifactJudgement(path, Classification.UNEXPECTED, "byte divergence, no policy rule")

    if rule.mode is RuleMode.EXEMPT:
        return ArtifactJudgement(
            path, Classification.EXPECTED, rule.note or f"exempt by rule '{rule.pattern}'"
        )

    if original is None or replayed is None:
        return ArtifactJudgement(
            path, Classification.UNEXPECTED, "content unavailable for content-level comparison"
        )

    if rule.mode is RuleMode.NORMALIZE:
        if not rule.strip_pattern:
            return ArtifactJudgement(path, Classification.UNEXPECTED, "normalize rule missing strip_pattern")
        stripped_a = re.sub(rule.strip_pattern, "", original)
        stripped_b = re.sub(rule.strip_pattern, "", replayed)
        if stripped_a == stripped_b:
            return ArtifactJudgement(
                path, Classification.EXPECTED, rule.note or "equal after normalization"
            )
        return ArtifactJudgement(path, Classification.UNEXPECTED, "differs after normalization")

    if rule.mode is RuleMode.NUMERIC_JSON:
        try:
            oa, ob = json.loads(original), json.loads(replayed)
        except json.JSONDecodeError as exc:
            return ArtifactJudgement(path, Classification.UNEXPECTED, f"not valid JSON: {exc}")
        ok, detail = _numeric_json_equal(oa, ob, rule.tolerance, rule.tolerance_type)
        if ok:
            return ArtifactJudgement(
                path, Classification.EXPECTED, rule.note or f"numbers within tolerance {rule.tolerance}"
            )
        return ArtifactJudgement(path, Classification.UNEXPECTED, detail)

    return ArtifactJudgement(path, Classification.UNEXPECTED, "unhandled rule mode")


def classify_unexpected_artifact(policy: NondeterminismPolicy, path: str) -> ArtifactJudgement:
    """An artifact produced by replay but absent from the certificate: exempt
    rules can still forgive it (e.g. a sometimes-emitted log)."""
    rule = match_rule(policy, path)
    if rule is not None and rule.mode is RuleMode.EXEMPT:
        return ArtifactJudgement(path, Classification.EXPECTED, rule.note or "exempt extra artifact")
    return ArtifactJudgement(path, Classification.UNEXPECTED_ARTIFACT, "produced but not certified")
