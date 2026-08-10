"""Evaluation: scoring and the harness-on/off table (design §12.3)."""

from __future__ import annotations

from .controlled import (
    PairedTaskDelta,
    ScoredExecution,
    StrategyGroundTruth,
    SystemScore,
    TaskScore,
    oracle_ground_truth,
    paired_task_deltas,
    render_comparison,
    score_system,
)
from .table import (
    ArmMetrics,
    HarnessRow,
    classify,
    render_table,
    run_comparison,
    score_arm,
)

__all__ = [
    "ArmMetrics",
    "HarnessRow",
    "PairedTaskDelta",
    "ScoredExecution",
    "StrategyGroundTruth",
    "SystemScore",
    "TaskScore",
    "classify",
    "oracle_ground_truth",
    "paired_task_deltas",
    "render_comparison",
    "render_table",
    "run_comparison",
    "score_arm",
    "score_system",
]
