"""Harness-on vs harness-off evaluation (design §8, §12.3).

Produces the one number: the false-verdict-rate delta between full Crucible and
a bare frontier agent (same LLM) on a stratified CORE-Bench slice. Also reports
decisiveness and task completion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ArmMetrics:
    false_verdict_rate: float
    decisiveness: float
    completion_rate: float
    n: int


@dataclass
class HarnessTable:
    harness_on: ArmMetrics
    harness_off: ArmMetrics
    stratum: str  # easy | medium | hard | all


def run_comparison(sample, model: str) -> list[HarnessTable]:
    """Week-4 kill-signal experiment + week-6 final table."""
    raise NotImplementedError("Stage 0, weeks 4 & 6.")
