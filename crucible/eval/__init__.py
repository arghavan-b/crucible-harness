"""Evaluation: scoring and the harness-on/off table (design §12.3)."""

from __future__ import annotations

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
    "classify",
    "render_table",
    "run_comparison",
    "score_arm",
]
