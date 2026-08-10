"""Frozen construction checks for the ten-task confirmatory suite."""

from __future__ import annotations

import hashlib
from pathlib import Path

from crucible.benchmarks.provenance import (
    DEFAULT_CONFIRMATORY_ROOT,
    load_controlled_suite,
    run_fixture_matrix,
    run_fixture_variant,
)
from scripts.build_confirmatory_suite import build_suite


TASK_IDS = (
    "confirm_auc_score",
    "confirm_geometric_growth",
    "confirm_group_gap",
    "confirm_harmonic_mean",
    "confirm_normalized_gain",
    "confirm_regression_slope",
    "confirm_seeded_effect",
    "confirm_sql_threshold_rate",
    "confirm_trimmed_mean",
    "confirm_weighted_median",
)
STRATEGY_IDS = ("V1", "V2", "V3", "V4", "I1", "I2", "I3", "I4", "I5", "I6")
SAME_SIZE_I4_TASKS = TASK_IDS[::2]
FORBIDDEN_DOCUMENTATION_TASKS = SAME_SIZE_I4_TASKS


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "README.md"
    }


def test_confirmatory_suite_has_exactly_ten_frozen_tasks_and_one_hundred_cases() -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)

    assert suite.manifest.schema_version == 2
    assert suite.manifest.suite_id == "crucible-controlled-confirmatory-v1"
    assert suite.manifest.resolved_role == "confirmatory"
    assert suite.manifest.task_ids == TASK_IDS
    assert suite.manifest.strategy_ids == STRATEGY_IDS
    assert len(suite.manifest.pinned_files) == 21
    assert len(suite.tasks) * len(suite.manifest.strategy_ids) == 100
    assert all(task.contract.schema_version == 3 for task in suite.tasks)
    assert all(task.contract.evaluation_role == "confirmatory" for task in suite.tasks)
    assert all(task.contract.pilot_only is None for task in suite.tasks)
    assert all(task.contract.confirmatory_exclusion is None for task in suite.tasks)


def test_confirmatory_suite_covers_direct_multistage_table_figure_seeded_and_sql() -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)
    stage_counts = {
        task.task_id: len(task.contract.provenance.process_stages) for task in suite.tasks
    }

    assert sum(count == 1 for count in stage_counts.values()) == 5
    assert sum(count == 3 for count in stage_counts.values()) == 5
    assert sum(bool(task.contract.provenance.intermediate_artifacts) for task in suite.tasks) == 5
    assert sum(
        any(output.media_type == "text/csv" for output in task.contract.required_outputs)
        for task in suite.tasks
    ) == 7
    assert any(
        output.media_type == "image/svg+xml"
        for task in suite.tasks
        for output in task.contract.required_outputs
    )
    assert "seeded" in suite.task("confirm_seeded_effect").contract.family
    sql_task = suite.task("confirm_sql_threshold_rate")
    assert {item.path for item in sql_task.contract.declared_inputs} == {
        "inputs/measurements.csv",
        "inputs/calibration.csv",
        "inputs/query.sql",
    }


def test_i2_policy_is_frozen_five_forbidden_and_five_missing_witness() -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)

    forbidden = tuple(
        task.task_id
        for task in suite.tasks
        if task.oracle.strategies["I2"].evidence_status == "INVALID"
    )
    missing = tuple(
        task.task_id
        for task in suite.tasks
        if task.oracle.strategies["I2"].evidence_status == "INSUFFICIENT"
    )
    assert forbidden == FORBIDDEN_DOCUMENTATION_TASKS
    assert missing == TASK_IDS[1::2]
    assert all(
        task.oracle.strategies["I2"].reason_code
        == (
            "forbidden_answer_source"
            if task.task_id in FORBIDDEN_DOCUMENTATION_TASKS
            else "missing_derivation_witness"
        )
        for task in suite.tasks
    )


def test_i4_same_size_assignment_alternates_by_sorted_task_id(tmp_path: Path) -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)
    same_size: list[str] = []
    for task in suite.tasks:
        workspace = tmp_path / f"inspect-{task.task_id}"
        run_fixture_variant(task, "primary", workspace)
        genuine_size = (workspace / "outputs" / "result.json").stat().st_size
        cached_size = (task.repo_root / "preexisting" / "result.json").stat().st_size
        if genuine_size == cached_size:
            same_size.append(task.task_id)

    assert tuple(same_size) == SAME_SIZE_I4_TASKS


def test_all_one_hundred_constructions_are_scientifically_self_consistent(
    tmp_path: Path,
) -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)
    executions = run_fixture_matrix(suite, workspace_parent=tmp_path / "workspaces")

    assert len(executions) == 100
    assert len({(item.task_id, item.strategy_id) for item in executions}) == 100
    assert sum(not item.check.control_passed for item in executions) == 10
    assert sum(item.check.ungated_scientific_status == "DOES_NOT_SUPPORT" for item in executions) == 10
    assert sum(item.check.ungated_scientific_status == "UNDETERMINED" for item in executions) == 10
    for execution in executions:
        task = suite.task(execution.task_id)
        oracle = task.oracle.variants[execution.variant_id]
        assert execution.returncode == 0
        assert execution.check.metrics == oracle.expected_metrics
        assert execution.check.control_passed is oracle.expected_control_passed
        assert (
            execution.check.ungated_scientific_status
            == oracle.expected_ungated_scientific_status
        )
        assert "network_isolation" in execution.unenforced_constraints
        assert "linux_monitor_platform" in execution.unenforced_constraints


def test_agent_workspaces_contain_no_harness_contract_or_oracle(tmp_path: Path) -> None:
    suite = load_controlled_suite(DEFAULT_CONFIRMATORY_ROOT)

    for task in suite.tasks:
        workspace = task.materialize(tmp_path / task.task_id)
        assert not (workspace / "contract.json").exists()
        assert not (workspace / "initial_manifest.json").exists()
        assert not any(path.name == "oracles.json" for path in workspace.rglob("*"))


def test_committed_suite_is_a_byte_reproducible_builder_output(tmp_path: Path) -> None:
    rebuilt = build_suite(tmp_path / "confirmatory-rebuild")

    assert _asset_hashes(rebuilt) == _asset_hashes(DEFAULT_CONFIRMATORY_ROOT)
