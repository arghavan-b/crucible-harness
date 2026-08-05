"""Benchmark harness: tasks, arms, and the harness-on/off comparison (design §12)."""

from __future__ import annotations

from .arms import Arm, ArmOutcome, HarnessOnArm, NaiveAgentArm, run_arm
from .corebench import BenchTask, GroundTruth, load_tasks, stratified_sample, synthetic_tasks
from .provenance import (
    ControlledTask,
    PilotSuite,
    PilotTaskError,
    load_pilot_suite,
    run_fixture_variant,
)
from .provenance_gate import evaluate_provenance, gate_certificate

__all__ = [
    "Arm",
    "ArmOutcome",
    "BenchTask",
    "ControlledTask",
    "GroundTruth",
    "HarnessOnArm",
    "NaiveAgentArm",
    "PilotSuite",
    "PilotTaskError",
    "evaluate_provenance",
    "gate_certificate",
    "load_pilot_suite",
    "load_tasks",
    "run_arm",
    "run_fixture_variant",
    "stratified_sample",
    "synthetic_tasks",
]
