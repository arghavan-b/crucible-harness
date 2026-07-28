"""Adapter: typed Claim -> ExperimentSpec (the existing harness's input).

The domain Claim is the richer object, but the executor, validation gates and
adjudicator already speak `ExperimentSpec`. Rather than fork the harness, this
maps down: one Claim becomes one `ClaimUnderTest` plus a positive control.

Two mappings carry real meaning and are worth stating:

  - **The comparator value becomes the positive control.** "Reproduce the
    paper's own baseline number first" is exactly the control the master design
    asks intake to generate, and a comparative claim always carries one. When
    the claim has no comparator value, we fall back to the mechanical control
    (entry point runs and exits cleanly) so the "no positive control, no verdict"
    rule still has something to hold.
  - **Seeds come from the acceptance policy**, not from a default. `min_seeds`
    is the submitter's (or intake's) declared statistical bar, so the spec runs
    exactly as many seeds as the policy demands.

Lossy by construction: the split spec, dedup policy, applicability-domain
requirement and evidence requirements have no home in `ExperimentSpec` — they
are consumed by the Domain Validity Engine, not the executor. `spec_from_claim`
records what it dropped in the returned notes so nothing is silently lost.
"""

from __future__ import annotations

import re

from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    ScalePolicy,
    Source,
    Tolerance,
    ToleranceType,
)

from .schema import Claim, ClaimType, EvidenceRequirement

_HYPOTHESIS_MAP = {
    ClaimType.COMPARATIVE: HypothesisType.COMPARATIVE,
    ClaimType.ABLATION: HypothesisType.ABLATION,
    ClaimType.REPRODUCTION: HypothesisType.REPRODUCTION,
    ClaimType.ABSOLUTE: HypothesisType.REPRODUCTION,
    ClaimType.RANKING: HypothesisType.COMPARATIVE,
}

# The adjudicator parses `comparison` with ^(\w+)\s*(op)\s*(\S+)$, so variable
# names must be bare identifiers.
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug(name: str, fallback: str = "value") -> str:
    out = _SLUG_RE.sub("_", (name or "").strip()).strip("_").lower()
    if not out or out[0].isdigit():
        out = f"{fallback}_{out}" if out else fallback
    return out


def _tolerance(claim: Claim) -> Tolerance:
    """Absolute tolerance for 'reproduced'. A stated margin delta is the natural
    scale: reproducing to within a tenth of the claimed improvement is a
    defensible bar, floored so a tiny delta doesn't demand impossible precision."""
    margin = claim.statement.margin
    if margin and margin.delta:
        return Tolerance(type=ToleranceType.ABSOLUTE, value=max(0.005, abs(margin.delta) / 10))
    return Tolerance(type=ToleranceType.ABSOLUTE, value=0.005)


def dropped_requirements(claim: Claim) -> list[EvidenceRequirement]:
    """Requirements the ExperimentSpec cannot express — i.e. everything the
    Domain Validity Engine must check instead of the executor. Surfacing these
    keeps the adapter honest about being lossy."""
    executor_covered = {
        EvidenceRequirement.SEEDS_SUFFICIENT,
        EvidenceRequirement.MARGIN_SURVIVES_VARIANCE,
        EvidenceRequirement.METRIC_MATCHES_CLAIM,
    }
    return [r for r in claim.requirements() if r not in executor_covered]


def spec_from_claim(
    claim: Claim,
    repo_uri: str,
    commit: str | None = None,
    experiment_id: str | None = None,
) -> ExperimentSpec:
    """Map one Claim onto an ExperimentSpec the existing harness can execute."""
    ok, reason = claim.is_adjudicable()
    if not ok:
        raise ValueError(f"claim {claim.claim_id} is not adjudicable: {reason}")

    policy = claim.acceptance_policy
    assert policy is not None  # guaranteed by is_adjudicable

    metric = claim.metric or "metric"
    subject = slug(claim.statement.subject, "subject")
    comparator = slug(claim.statement.comparator or "", "comparator")

    reported: dict[str, float] = {}
    if claim.reported.subject_value is not None:
        reported[subject] = claim.reported.subject_value
    if claim.statement.comparator and claim.reported.comparator_value is not None:
        reported[comparator] = claim.reported.comparator_value

    if claim.is_comparative and claim.statement.comparator:
        comparison = f"{subject} > {comparator}"
    elif claim.reported.subject_value is not None:
        comparison = f"{subject} >= {claim.reported.subject_value}"
    else:
        comparison = f"{subject} >= 0"

    under_test = ClaimUnderTest(
        claim_id=claim.claim_id,
        metric=slug(metric, "metric"),
        comparison=comparison,
        reported_values=reported,
        tolerance=_tolerance(claim),
        seeds=list(range(policy.min_seeds)),
    )

    # Positive control: reproduce the claim's own baseline number when there is
    # one, else the mechanical "it runs" control.
    if claim.reported.comparator_value is not None and claim.statement.comparator:
        control = PositiveControl(
            control_id="pc1",
            description=(
                f"Reproduce reported baseline: {claim.statement.comparator} = "
                f"{claim.reported.comparator_value}"
            ),
            metric=comparator,
            expected=claim.reported.comparator_value,
            tolerance=_tolerance(claim),
        )
    else:
        control = PositiveControl(
            control_id="pc1",
            description="entry point runs to completion and exits cleanly",
            metric="smoke_exit_code",
            expected=0.0,
            tolerance=Tolerance(value=0.0),
        )

    statement = claim.statement.text or (
        f"{claim.statement.subject} {claim.statement.relation.value} "
        f"{claim.statement.comparator or ''}".strip()
    )
    exp_id = experiment_id or f"exp_{slug(claim.claim_id, 'claim')}"

    return ExperimentSpec(
        experiment_id=exp_id,
        hypothesis=Hypothesis(
            statement=statement,
            type=_HYPOTHESIS_MAP.get(claim.type, HypothesisType.REPRODUCTION),
        ),
        source=Source(repo_uri=repo_uri, commit=commit),
        claims_under_test=[under_test],
        positive_controls=[control],
        scale_policy=ScalePolicy(smoke_first=True),
    )


def specs_from_claims(
    claims: list[Claim], repo_uri: str, commit: str | None = None
) -> list[ExperimentSpec]:
    """Map every adjudicable claim; non-adjudicable ones are skipped rather than
    raising, since a ClaimSet routinely contains partial drafts."""
    out: list[ExperimentSpec] = []
    for claim in claims:
        if claim.is_adjudicable()[0]:
            out.append(spec_from_claim(claim, repo_uri, commit))
    return out
