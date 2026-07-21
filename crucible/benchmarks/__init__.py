"""Benchmark harness: tasks, arms, and the harness-on/off comparison (design §12)."""

from __future__ import annotations

from .arms import Arm, ArmOutcome, HarnessOnArm, NaiveAgentArm, run_arm
from .corebench import BenchTask, GroundTruth, load_tasks, stratified_sample, synthetic_tasks

__all__ = [
    "Arm",
    "ArmOutcome",
    "BenchTask",
    "GroundTruth",
    "HarnessOnArm",
    "NaiveAgentArm",
    "load_tasks",
    "run_arm",
    "stratified_sample",
    "synthetic_tasks",
]
