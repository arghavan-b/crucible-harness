"""Benchmark arms (design §12, §24).

An arm turns a task into a claimed outcome. The comparison holds the LLM constant
and toggles only the harness:

  - HarnessOnArm: the full Crucible pipeline (run_pipeline) — plan, validate,
    execute, verify, adjudicate. Emits a calibrated verdict.
  - NaiveAgentArm: a bare agent stand-in — runs the entry point and reports
    SUCCESS without verification. This is the deterministic placeholder for the
    real harness-off baseline (an LLM agent with no harness); swap it for a real
    model-backed arm to get the true contrast.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Protocol

from crucible.benchmarks.corebench import BenchTask
from crucible.pipeline import run_pipeline
from crucible.schemas import VerdictStatus


@dataclass
class ArmOutcome:
    task_id: str
    verdict_status: VerdictStatus
    answer: dict[str, float] = field(default_factory=dict)
    completed: bool = False
    detail: str | None = None


class Arm(Protocol):
    name: str
    def run(self, task: BenchTask, workdir: str) -> ArmOutcome: ...


def _answer_from_verdict(verdict) -> dict[str, float]:
    result = verdict.evidence.result
    if result is None or not result.observed:
        return {}
    from statistics import fmean
    return {k: fmean(v) for k, v in result.observed.items() if v}


class HarnessOnArm:
    name = "crucible"

    def run(self, task: BenchTask, workdir: str) -> ArmOutcome:
        repo = os.path.join(workdir, "repo")
        os.makedirs(repo, exist_ok=True)
        task.materialize(repo)
        db = os.path.join(workdir, "trace.sqlite")
        try:
            result = run_pipeline(repo, db_path=db)
        except Exception as exc:  # a rejected plan or harness error is an honest non-verdict
            return ArmOutcome(task.task_id, VerdictStatus.INCONCLUSIVE, completed=False,
                              detail=f"pipeline error: {exc}")
        return ArmOutcome(
            task_id=task.task_id,
            verdict_status=result.verdict.status,
            answer=_answer_from_verdict(result.verdict),
            completed=result.run.all_succeeded,
            detail=result.verdict.reason,
        )


class NaiveAgentArm:
    """Runs the entry point and always claims SUCCESS — no verification."""

    name = "bare-agent"

    def run(self, task: BenchTask, workdir: str) -> ArmOutcome:
        repo = os.path.join(workdir, "repo")
        os.makedirs(repo, exist_ok=True)
        task.materialize(repo)
        entry = next((f for f in ("inference.py", "main.py", "train.py", "run.py")
                      if os.path.exists(os.path.join(repo, f))), None)
        if entry:
            try:
                subprocess.run(["python3", entry], cwd=repo, capture_output=True, timeout=60)
            except Exception:
                pass
        answer = self._scrape(repo)
        # The defining weakness: it declares success regardless of what happened.
        return ArmOutcome(task.task_id, VerdictStatus.SUCCESS, answer=answer, completed=True,
                          detail="claimed success without verification")

    def _scrape(self, repo: str) -> dict[str, float]:
        path = os.path.join(repo, "outputs", "metrics.json")
        try:
            data = json.load(open(path, encoding="utf-8"))
            return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}


def run_arm(arm: Arm, tasks: list[BenchTask]) -> dict[str, ArmOutcome]:
    outcomes: dict[str, ArmOutcome] = {}
    for task in tasks:
        with tempfile.TemporaryDirectory(prefix="crucible_bench_") as wd:
            outcomes[task.task_id] = arm.run(task, wd)
    return outcomes
