"""Benchmark harness: tasks, scoring, and the harness-on/off comparison (design §12)."""

from __future__ import annotations

from crucible.benchmarks import (
    BenchTask,
    GroundTruth,
    HarnessOnArm,
    NaiveAgentArm,
    load_tasks,
    run_arm,
    stratified_sample,
    synthetic_tasks,
)
from crucible.benchmarks.arms import ArmOutcome
from crucible.eval import classify, render_table, run_comparison, score_arm
from crucible.schemas import VerdictStatus


# --- tasks --------------------------------------------------------------------


def test_synthetic_tasks_have_both_classes() -> None:
    tasks = load_tasks()
    assert any(t.ground_truth.reproduces for t in tasks)
    assert any(not t.ground_truth.reproduces for t in tasks)
    assert {t.difficulty for t in tasks} == {"easy", "medium", "hard"}


def test_stratified_sample_is_balanced_and_deterministic() -> None:
    tasks = synthetic_tasks()
    a = stratified_sample(tasks, 3)
    b = stratified_sample(tasks, 3)
    assert [t.task_id for t in a] == [t.task_id for t in b]
    assert {t.difficulty for t in a} == {"easy", "medium", "hard"}  # one per stratum


# --- scoring ------------------------------------------------------------------


def _task(reproduces, expected=None):
    return BenchTask("t", "easy", GroundTruth(reproduces=reproduces, expected=expected or {}))


def test_classify_cases() -> None:
    good = _task(True, {"accuracy": 0.9})
    bad = _task(False)
    S, RN, EF = VerdictStatus.SUCCESS, VerdictStatus.RESULT_NEGATIVE, VerdictStatus.EXECUTION_FAILURE
    assert classify(good, ArmOutcome("t", S, {"accuracy": 0.9})) == "correct"
    assert classify(good, ArmOutcome("t", S, {"accuracy": 0.5})) == "false_success"
    assert classify(bad, ArmOutcome("t", S)) == "false_success"
    assert classify(bad, ArmOutcome("t", RN)) == "correct"
    assert classify(good, ArmOutcome("t", RN)) == "false_negative"
    assert classify(bad, ArmOutcome("t", EF)) == "inconclusive"  # honest


def test_score_arm_counts_false_verdicts() -> None:
    tasks = [_task(False), _task(False)]
    outcomes = {"t": ArmOutcome("t", VerdictStatus.SUCCESS)}  # both share id "t"
    m = score_arm("x", "all", tasks, outcomes)
    assert m.false_verdict_rate == 1.0
    assert m.decisiveness == 1.0
    assert m.correctness == 0.0


# --- end-to-end comparison (real harness-on arm) -----------------------------


def test_harness_on_arm_verdicts() -> None:
    tasks = {t.task_id: t for t in synthetic_tasks()}
    arm = HarnessOnArm()
    import tempfile

    with tempfile.TemporaryDirectory() as wd1:
        ok = arm.run(tasks["easy_ok_1"], wd1)
    with tempfile.TemporaryDirectory() as wd2:
        broken = arm.run(tasks["easy_broken_1"], wd2)
    assert ok.verdict_status is VerdictStatus.SUCCESS
    assert broken.verdict_status is VerdictStatus.EXECUTION_FAILURE


def test_comparison_shows_harness_reduces_false_verdicts() -> None:
    tasks = synthetic_tasks()
    on = run_arm(HarnessOnArm(), tasks)
    off = run_arm(NaiveAgentArm(), tasks)
    rows = run_comparison(tasks, on, off)
    overall = next(r for r in rows if r.stratum == "all")

    # Crucible never emits a false verdict on these tasks; the bare agent does
    # (it declares SUCCESS on the broken repos it never verified).
    assert overall.harness_on.false_verdict_rate == 0.0
    assert overall.harness_off.false_verdict_rate > 0.0
    assert overall.false_verdict_delta > 0.0
    assert isinstance(render_table(rows), str)
