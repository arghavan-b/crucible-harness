"""CORE-Bench harness (design §12.2).

Primary benchmark (270 tasks, 90 papers, 3 disciplines): directly measures
install/execute/interpret. Report harness-on vs harness-off with an identical
LLM. Selection is a deterministic, versioned, stratified sample.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BenchTask:
    task_id: str
    repo_uri: str
    difficulty: str  # easy | medium | hard


def load_tasks() -> list[BenchTask]:
    raise NotImplementedError("Stage 0, week 4.")


def stratified_sample(n: int, seed: int = 0) -> list[BenchTask]:
    """Deterministic stratified (easy/medium/hard) sample of size n."""
    raise NotImplementedError("Stage 0, week 4.")
