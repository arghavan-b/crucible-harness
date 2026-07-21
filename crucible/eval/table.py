"""Harness-on vs harness-off evaluation (design §8, §12.3).

Produces the one number: the false-verdict-rate delta between full Crucible and a
bare agent (same LLM) on a stratified slice. A false verdict is a decisive verdict
(SUCCESS / RESULT_NEGATIVE) that is wrong — a false SUCCESS is fabricated positive
science, a false RESULT_NEGATIVE is fabricated negative science. INCONCLUSIVE and
EXECUTION_FAILURE are honest non-answers: they cost decisiveness, not correctness.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from crucible.benchmarks.arms import ArmOutcome
from crucible.benchmarks.corebench import BenchTask
from crucible.schemas import VerdictStatus

DECISIVE = {VerdictStatus.SUCCESS, VerdictStatus.RESULT_NEGATIVE}


def _answer_wrong(answer: dict[str, float], expected: dict[str, float], tol: float = 1e-6) -> bool:
    for k, want in expected.items():
        if k in answer and abs(answer[k] - want) > tol:
            return True
    return False


def classify(task: BenchTask, o: ArmOutcome) -> str:
    gt = task.ground_truth
    s = o.verdict_status
    if s not in DECISIVE:
        return "inconclusive"          # honest non-answer (incl. execution failure)
    if s is VerdictStatus.SUCCESS:
        if not gt.reproduces or _answer_wrong(o.answer, gt.expected):
            return "false_success"
        return "correct"
    # RESULT_NEGATIVE
    return "correct" if not gt.reproduces else "false_negative"


@dataclass
class ArmMetrics:
    arm: str
    stratum: str
    n: int
    false_verdict_rate: float
    decisiveness: float
    correctness: float
    counts: dict[str, int] = field(default_factory=dict)


def score_arm(arm_name: str, stratum: str, tasks: list[BenchTask],
              outcomes: dict[str, ArmOutcome]) -> ArmMetrics:
    n = len(tasks)
    counts = {"correct": 0, "false_success": 0, "false_negative": 0, "inconclusive": 0}
    decisive = 0
    for t in tasks:
        o = outcomes[t.task_id]
        counts[classify(t, o)] += 1
        if o.verdict_status in DECISIVE:
            decisive += 1
    false_verdicts = counts["false_success"] + counts["false_negative"]
    return ArmMetrics(
        arm=arm_name, stratum=stratum, n=n,
        false_verdict_rate=false_verdicts / n if n else 0.0,
        decisiveness=decisive / n if n else 0.0,
        correctness=counts["correct"] / n if n else 0.0,
        counts=counts,
    )


@dataclass
class HarnessRow:
    stratum: str
    harness_on: ArmMetrics
    harness_off: ArmMetrics

    @property
    def false_verdict_delta(self) -> float:
        return self.harness_off.false_verdict_rate - self.harness_on.false_verdict_rate


def run_comparison(
    tasks: list[BenchTask],
    on: dict[str, ArmOutcome],
    off: dict[str, ArmOutcome],
    on_name: str = "crucible",
    off_name: str = "bare-agent",
) -> list[HarnessRow]:
    strata: dict[str, list[BenchTask]] = {}
    for t in tasks:
        strata.setdefault(t.difficulty, []).append(t)

    rows: list[HarnessRow] = []
    for stratum in sorted(strata):
        st = strata[stratum]
        rows.append(HarnessRow(
            stratum=stratum,
            harness_on=score_arm(on_name, stratum, st, on),
            harness_off=score_arm(off_name, stratum, st, off),
        ))
    rows.append(HarnessRow(
        stratum="all",
        harness_on=score_arm(on_name, "all", tasks, on),
        harness_off=score_arm(off_name, "all", tasks, off),
    ))
    return rows


def render_table(rows: list[HarnessRow]) -> str:
    head = f"{'stratum':10} {'n':>3}  {'arm':10} {'false-verdict':>14} {'decisive':>9} {'correct':>8}"
    lines = [head, "-" * len(head)]
    for row in rows:
        for m in (row.harness_on, row.harness_off):
            lines.append(
                f"{row.stratum:10} {m.n:>3}  {m.arm:10} "
                f"{m.false_verdict_rate:>13.0%} {m.decisiveness:>9.0%} {m.correctness:>8.0%}"
            )
        lines.append(f"{'':10} {'':>3}  {'Δ false-verdict':10} {row.false_verdict_delta:>13.0%}")
    return "\n".join(lines)
