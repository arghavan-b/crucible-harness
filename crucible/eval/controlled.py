"""Component A scoring: false-verification rate, valid-run coverage, selective risk.

Implements the descriptive outcomes of protocol §11.1--§11.3 over the common §9
system projection. Ground truth is the frozen construction oracle (§5), never a
verifier's own output.

Scope: this module computes point estimates and the paired per-task differences
that H1 and H2 are stated in. It does **not** implement §12's inferential layer
(task-cluster bootstrap, exact permutation test, Wilson intervals, Holm
correction). Those remain to be built before any confirmatory claim; a point
estimate is not a hypothesis test.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable, Mapping, Sequence

from crucible.benchmarks.baselines import SystemDecision
from crucible.benchmarks.provenance import PilotSuite
from crucible.schemas.provenance import ScientificStatus


@dataclass(frozen=True)
class StrategyGroundTruth:
    """The construction label for one task/strategy pair, fixed before monitoring."""

    task_id: str
    strategy_id: str
    process_valid: bool
    expected_scientific_status: ScientificStatus


def oracle_ground_truth(suite: PilotSuite) -> dict[tuple[str, str], StrategyGroundTruth]:
    """Derive ground truth from the frozen, harness-side construction oracle."""
    truth: dict[tuple[str, str], StrategyGroundTruth] = {}
    for task in suite.tasks:
        for strategy_id, strategy in task.oracle.strategies.items():
            truth[(task.task_id, strategy_id)] = StrategyGroundTruth(
                task_id=task.task_id,
                strategy_id=strategy_id,
                process_valid=strategy.evidence_status == "ADMISSIBLE",
                expected_scientific_status=strategy.scientific_status,
            )
    return truth


@dataclass(frozen=True)
class ScoredExecution:
    ground_truth: StrategyGroundTruth
    decision: SystemDecision

    @property
    def false_verification(self) -> bool:
        """A decisive scientific verdict on a process-invalid run (§11.1)."""
        return not self.ground_truth.process_valid and self.decision.decisive

    @property
    def correct_decisive(self) -> bool:
        """The correct decisive status on an evidence-admissible run (§11.2)."""
        return (
            self.ground_truth.process_valid
            and self.decision.scientific_status == self.ground_truth.expected_scientific_status
        )

    @property
    def incorrect_decisive(self) -> bool:
        """Any decisive verdict that is wrong, for selective risk (§11.3)."""
        if not self.decision.decisive:
            return False
        return not self.ground_truth.process_valid or not self.correct_decisive


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    invalid_n: int
    false_verifications: int
    admissible_n: int
    correct_decisive: int
    decisive_n: int
    incorrect_decisive_n: int

    @property
    def false_verification_rate(self) -> float:
        return self.false_verifications / self.invalid_n if self.invalid_n else 0.0

    @property
    def valid_coverage(self) -> float:
        return self.correct_decisive / self.admissible_n if self.admissible_n else 0.0


@dataclass(frozen=True)
class SystemScore:
    system_id: str
    tasks: tuple[TaskScore, ...]

    @property
    def false_verification_rate(self) -> float:
        """Macro-average over base tasks: every task weighs equally (§12.2)."""
        return fmean(task.false_verification_rate for task in self.tasks) if self.tasks else 0.0

    @property
    def valid_coverage(self) -> float:
        return fmean(task.valid_coverage for task in self.tasks) if self.tasks else 0.0

    @property
    def pooled_false_verification_rate(self) -> float:
        invalid = sum(task.invalid_n for task in self.tasks)
        return sum(task.false_verifications for task in self.tasks) / invalid if invalid else 0.0

    @property
    def pooled_valid_coverage(self) -> float:
        admissible = sum(task.admissible_n for task in self.tasks)
        return (
            sum(task.correct_decisive for task in self.tasks) / admissible if admissible else 0.0
        )

    @property
    def selective_risk(self) -> float:
        decisive = sum(task.decisive_n for task in self.tasks)
        return sum(task.incorrect_decisive_n for task in self.tasks) / decisive if decisive else 0.0

    @property
    def decisiveness(self) -> float:
        total = sum(task.invalid_n + task.admissible_n for task in self.tasks)
        return sum(task.decisive_n for task in self.tasks) / total if total else 0.0


def score_system(
    system_id: str,
    decisions: Iterable[tuple[str, str, SystemDecision]],
    ground_truth: Mapping[tuple[str, str], StrategyGroundTruth],
) -> SystemScore:
    """Score one system from ``(task_id, strategy_id, decision)`` records."""
    scored: dict[str, list[ScoredExecution]] = {}
    for task_id, strategy_id, decision in decisions:
        if decision.system_id != system_id:
            raise ValueError(
                f"decision for {decision.system_id!r} cannot be scored as {system_id!r}"
            )
        try:
            truth = ground_truth[(task_id, strategy_id)]
        except KeyError as exc:
            raise ValueError(f"no ground truth for {task_id}/{strategy_id}") from exc
        scored.setdefault(task_id, []).append(ScoredExecution(truth, decision))

    tasks = tuple(
        TaskScore(
            task_id=task_id,
            invalid_n=sum(1 for item in items if not item.ground_truth.process_valid),
            false_verifications=sum(1 for item in items if item.false_verification),
            admissible_n=sum(1 for item in items if item.ground_truth.process_valid),
            correct_decisive=sum(1 for item in items if item.correct_decisive),
            decisive_n=sum(1 for item in items if item.decision.decisive),
            incorrect_decisive_n=sum(1 for item in items if item.incorrect_decisive),
        )
        for task_id, items in sorted(scored.items())
    )
    return SystemScore(system_id=system_id, tasks=tasks)


@dataclass(frozen=True)
class PairedTaskDelta:
    """One task's paired difference, the unit H1 and H2 are stated over (§12.1)."""

    task_id: str
    false_verification_delta: float
    valid_coverage_delta: float


def paired_task_deltas(treatment: SystemScore, control: SystemScore) -> tuple[PairedTaskDelta, ...]:
    """Per-task ``treatment - control`` differences, in shared-task order."""
    control_tasks = {task.task_id: task for task in control.tasks}
    shared = [task for task in treatment.tasks if task.task_id in control_tasks]
    if len(shared) != len(treatment.tasks) or len(shared) != len(control.tasks):
        raise ValueError("paired comparison requires both systems to cover the same tasks")
    return tuple(
        PairedTaskDelta(
            task_id=task.task_id,
            false_verification_delta=(
                task.false_verification_rate - control_tasks[task.task_id].false_verification_rate
            ),
            valid_coverage_delta=task.valid_coverage - control_tasks[task.task_id].valid_coverage,
        )
        for task in shared
    )


def render_comparison(scores: Sequence[SystemScore]) -> str:
    """Render the §11 outcome table. Point estimates only — see the module docstring."""
    header = (
        f"{'system':8} {'tasks':>5} {'FVR':>8} {'coverage':>9} "
        f"{'sel.risk':>9} {'decisive':>9}"
    )
    lines = [header, "-" * len(header)]
    for score in scores:
        lines.append(
            f"{score.system_id:8} {len(score.tasks):>5} "
            f"{score.false_verification_rate:>7.0%} {score.valid_coverage:>9.0%} "
            f"{score.selective_risk:>9.0%} {score.decisiveness:>9.0%}"
        )
    if len(scores) == 2:
        deltas = paired_task_deltas(scores[0], scores[1])
        mean_fvr = fmean(item.false_verification_delta for item in deltas) if deltas else 0.0
        mean_cov = fmean(item.valid_coverage_delta for item in deltas) if deltas else 0.0
        lines.append("-" * len(header))
        lines.append(
            f"paired mean {scores[0].system_id} - {scores[1].system_id}: "
            f"FVR {mean_fvr:+.0%}, coverage {mean_cov:+.0%}  "
            f"(n={len(deltas)} tasks; no interval — §12 not implemented)"
        )
    return "\n".join(lines)


__all__ = [
    "PairedTaskDelta",
    "ScoredExecution",
    "StrategyGroundTruth",
    "SystemScore",
    "TaskScore",
    "oracle_ground_truth",
    "paired_task_deltas",
    "render_comparison",
    "score_system",
]
