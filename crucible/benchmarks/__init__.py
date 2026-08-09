"""Benchmark harness: tasks, arms, and the harness-on/off comparison (design §12)."""

from __future__ import annotations

from .arms import Arm, ArmOutcome, HarnessOnArm, NaiveAgentArm, run_arm
from .corebench import BenchTask, GroundTruth, load_tasks, stratified_sample, synthetic_tasks
from .provenance import (
    ControlledTask,
    OracleComparison,
    PilotSuite,
    PilotTaskError,
    StrategyWorkspace,
    clean_strategy_workspace,
    compare_gate_decision_to_oracle,
    load_pilot_suite,
    run_fixture_matrix,
    run_fixture_strategy,
    run_fixture_variant,
)
from .provenance_gate import evaluate_provenance, gate_certificate
from .provenance_container import (
    LinuxContainerExecution,
    ProvenanceRunMetrics,
    build_linux_capture_argv,
    ensure_linux_provenance_image,
    run_frozen_strategy_in_container,
)

__all__ = [
    "Arm",
    "ArmOutcome",
    "BenchTask",
    "ControlledTask",
    "GroundTruth",
    "HarnessOnArm",
    "LinuxContainerExecution",
    "NaiveAgentArm",
    "OracleComparison",
    "PilotSuite",
    "PilotTaskError",
    "ProvenanceRunMetrics",
    "StrategyWorkspace",
    "clean_strategy_workspace",
    "compare_gate_decision_to_oracle",
    "evaluate_provenance",
    "build_linux_capture_argv",
    "ensure_linux_provenance_image",
    "gate_certificate",
    "load_pilot_suite",
    "load_tasks",
    "run_arm",
    "run_fixture_matrix",
    "run_fixture_strategy",
    "run_fixture_variant",
    "run_frozen_strategy_in_container",
    "stratified_sample",
    "synthetic_tasks",
]
