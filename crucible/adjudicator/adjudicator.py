"""Verdict Adjudicator — a decision procedure, not a model (design §8).

Per-claim decision procedure (§8.1), evaluated strictly in order:

  1. Positive control passed?  no  -> INCONCLUSIVE(control_failed). Stop.
     (No positive control at all -> INCONCLUSIVE(no_positive_control).)
  2. All steps SUCCEEDED with gating verifiers passed?
     no -> EXECUTION_FAILURE(deepest cause). Stop.
  3. Did any repair touch the scientific path (model/data/eval)?
     yes -> INCONCLUSIVE(scientific_path_modified) unless a human approved it.
     (Stage 0 runs no repairs, so this is structurally present but inert.)
  4. Compare the observed metric to the spec across seeds:
       - reproduction hypothesis -> within tolerance of the reported value?
       - comparative/ablation    -> is the claimed inequality statistically
                                     supported (one-sided Welch t-test)?
     -> SUCCESS | RESULT_NEGATIVE.

SUCCESS / RESULT_NEGATIVE are only reachable once the control passed, every step
verified, and no unresolved INCONCLUSIVE condition remains. INCONCLUSIVE is the
default under uncertainty — the system is rewarded for saying "I don't know".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import fmean

from crucible.executor.executor import RunResult
from crucible.schemas import (
    ClaimUnderTest,
    Evidence,
    ExecutionIntegrity,
    ExperimentSpec,
    HypothesisType,
    PositiveControlEvidence,
    Provenance,
    ResultEvidence,
    StepState,
    Tolerance,
    ToleranceType,
    Verdict,
    VerdictStatus,
)

from .stats import one_sample_t_test, welch_t_test


@dataclass
class Observations:
    """Measured values fed to the adjudicator. In production these come from an
    evaluate_claims step / metrics extractor; here they are passed explicitly so
    adjudication is unit-testable."""

    claim_series: dict[str, list[float]] = field(default_factory=dict)  # variable -> per-seed values
    control_values: dict[str, float] = field(default_factory=dict)      # control_id -> observed


_COMPARISON_RE = re.compile(r"^\s*(\w+)\s*(>=|<=|==|>|<)\s*(\S+)\s*$")


def _within(observed: float, expected: float, tol: Tolerance) -> bool:
    if tol.type is ToleranceType.RELATIVE:
        denom = max(abs(expected), 1e-12)
        return abs(observed - expected) / denom <= tol.value
    return abs(observed - expected) <= tol.value


def _clamp(x: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, x))


def _mk(
    spec: ExperimentSpec,
    run: RunResult,
    claim_id: str,
    status: VerdictStatus,
    confidence: float,
    evidence: Evidence,
    reason: str | None = None,
) -> Verdict:
    return Verdict(
        experiment_id=spec.experiment_id,
        claim_id=claim_id,
        status=status,
        confidence=confidence,
        reason=reason,
        evidence=evidence,
        provenance=Provenance(
            trace_id=run.trace_id, replay_command=f"crucible replay {run.trace_id}"
        ),
    )


def adjudicate(
    spec: ExperimentSpec,
    run: RunResult,
    claim_id: str,
    observations: Observations | None = None,
    *,
    alpha: float = 0.05,
    scientific_repairs_approved: bool = False,
) -> Verdict:
    observations = observations or Observations()
    ev = Evidence()

    # --- Step 1: positive control -------------------------------------------
    if not spec.positive_controls:
        return _mk(spec, run, claim_id, VerdictStatus.INCONCLUSIVE, 0.9, ev, "no_positive_control")
    for pc in spec.positive_controls:
        observed = observations.control_values.get(pc.control_id)
        if observed is None:
            ev.positive_control = PositiveControlEvidence(status="not_measured")
            return _mk(spec, run, claim_id, VerdictStatus.INCONCLUSIVE, 0.8, ev,
                       f"control_not_measured:{pc.control_id}")
        passed = _within(observed, pc.expected, pc.tolerance)
        ev.positive_control = PositiveControlEvidence(
            status="passed" if passed else "failed", observed=observed
        )
        if not passed:
            return _mk(spec, run, claim_id, VerdictStatus.INCONCLUSIVE, 0.95, ev, "control_failed")

    # --- Step 2: execution integrity ----------------------------------------
    repairs = _collect_repairs(run)
    ev.execution_integrity = ExecutionIntegrity(
        all_steps_verified=run.all_succeeded, repairs_applied=[r for r, _sci in repairs]
    )
    if not run.all_succeeded:
        cause = _deepest_failure(run)
        return _mk(spec, run, claim_id, VerdictStatus.EXECUTION_FAILURE, 1.0, ev, cause)

    # --- Step 3: scientific-path repairs ------------------------------------
    scientific_repairs = [r for r, sci in repairs if sci]
    if scientific_repairs and not scientific_repairs_approved:
        ev.execution_integrity.repair_risk_note = (
            f"repairs touched the scientific path: {', '.join(scientific_repairs)}"
        )
        return _mk(spec, run, claim_id, VerdictStatus.INCONCLUSIVE, 0.9, ev,
                   "scientific_path_modified")

    # --- Step 4: claim comparison -------------------------------------------
    claim = next((c for c in spec.claims_under_test if c.claim_id == claim_id), None)
    if claim is None:
        return _mk(spec, run, claim_id, VerdictStatus.INCONCLUSIVE, 0.9, ev, f"unknown_claim:{claim_id}")

    if spec.hypothesis.type in (HypothesisType.COMPARATIVE, HypothesisType.ABLATION):
        status, conf, result, reason = _eval_comparative(claim, observations, alpha)
    else:  # REPRODUCTION, EXPLORATORY
        status, conf, result, reason = _eval_reproduction(claim, observations)

    ev.result = result
    return _mk(spec, run, claim_id, status, conf, ev, reason)


# --- claim evaluators ---------------------------------------------------------


def _eval_reproduction(
    claim: ClaimUnderTest, obs: Observations
) -> tuple[VerdictStatus, float, ResultEvidence, str | None]:
    if not claim.reported_values:
        return (VerdictStatus.INCONCLUSIVE, 0.8,
                ResultEvidence(conclusion="no reported reference value"), "no_reference_value")

    observed_map: dict[str, list[float]] = {}
    reproduced = True
    margins: list[float] = []
    for var, reported in claim.reported_values.items():
        series = obs.claim_series.get(var)
        if not series:
            return (VerdictStatus.INCONCLUSIVE, 0.8,
                    ResultEvidence(observed=observed_map), f"metric_not_measured:{var}")
        observed_map[var] = series
        mean = fmean(series)
        if not _within(mean, reported, claim.tolerance):
            reproduced = False
        if claim.tolerance.value > 0:
            margins.append(1 - abs(mean - reported) / claim.tolerance.value)

    status = VerdictStatus.SUCCESS if reproduced else VerdictStatus.RESULT_NEGATIVE
    conf = _clamp(min(margins)) if margins and reproduced else 0.9
    result = ResultEvidence(
        observed=observed_map,
        test=f"reproduction within tolerance {claim.tolerance.value} ({claim.tolerance.type.value})",
        conclusion="reproduced reported value(s)" if reproduced else "did not reproduce reported value(s)",
    )
    return status, conf, result, None


def _eval_comparative(
    claim: ClaimUnderTest, obs: Observations, alpha: float
) -> tuple[VerdictStatus, float, ResultEvidence, str | None]:
    m = _COMPARISON_RE.match(claim.comparison)
    if not m:
        return (VerdictStatus.INCONCLUSIVE, 0.8,
                ResultEvidence(conclusion=f"unparseable comparison {claim.comparison!r}"),
                "unparseable_comparison")
    lhs, op, rhs = m.group(1), m.group(2), m.group(3)

    a = obs.claim_series.get(lhs)
    if not a:
        return (VerdictStatus.INCONCLUSIVE, 0.8, ResultEvidence(), f"metric_not_measured:{lhs}")

    observed_map: dict[str, list[float]] = {lhs: a}
    rhs_is_number = _is_number(rhs)
    b: list[float] | None = None
    if not rhs_is_number:
        b = obs.claim_series.get(rhs)
        if not b:
            return (VerdictStatus.INCONCLUSIVE, 0.8, ResultEvidence(observed=observed_map),
                    f"metric_not_measured:{rhs}")
        observed_map[rhs] = b

    if len(a) < 2 or (b is not None and len(b) < 2):
        return (VerdictStatus.INCONCLUSIVE, 0.7, ResultEvidence(observed=observed_map),
                "insufficient_seeds_for_significance")

    tt = one_sample_t_test(a, float(rhs)) if rhs_is_number else welch_t_test(a, b)  # type: ignore[arg-type]
    if op in (">", ">="):
        p_one, holds = tt.p_greater(), tt.mean_a > tt.mean_b
    elif op in ("<", "<="):
        p_one, holds = tt.p_less(), tt.mean_a < tt.mean_b
    else:  # ==  -> "reproduced/indistinguishable" reads as not-significantly-different
        p_one, holds = tt.p_two_sided, abs(tt.mean_a - tt.mean_b) <= 0

    supported = holds and p_one < alpha
    status = VerdictStatus.SUCCESS if supported else VerdictStatus.RESULT_NEGATIVE
    conf = _clamp(1 - p_one) if supported else _clamp(p_one)
    result = ResultEvidence(
        observed=observed_map,
        test=f"one-sided Welch t, p={p_one:.2g}, alpha={alpha}",
        conclusion=(
            f"{lhs} {op} {rhs} supported" if supported
            else f"{lhs} not distinguishable from {rhs} at alpha={alpha}"
        ),
    )
    return status, conf, result, None


# --- helpers ------------------------------------------------------------------


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _deepest_failure(run: RunResult) -> str:
    for r in reversed(run.step_results):
        if r.state is not StepState.SUCCEEDED:
            return r.failure_reason or f"{r.step_id} did not succeed"
    return "unknown_execution_failure"


def _collect_repairs(run: RunResult) -> list[tuple[str, bool]]:
    """Return (repair_id, touched_scientific_path) pairs. Stage 0 has no
    recovery so this is empty, but the adjudicator honors it if present."""
    repairs: list[tuple[str, bool]] = []
    for r in run.step_results:
        for rep in getattr(r, "repairs", []) or []:
            repairs.append((str(rep), bool(getattr(rep, "scientific", False))))
    return repairs
