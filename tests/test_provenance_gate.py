"""Deterministic evidence-gate tests over the frozen V1--I6 strategy matrix."""

from __future__ import annotations

from pathlib import Path

import pytest

from crucible.benchmarks.provenance import (
    compare_gate_decision_to_oracle,
    load_pilot_suite,
    run_fixture_variant,
)
from crucible.benchmarks.provenance_gate import evaluate_provenance, gate_certificate
from crucible.schemas.provenance import ProvenanceGateDecision
from crucible.trace.capture import WorkspaceDigestSnapshot

from synthetic_certificates import (
    STRATEGY_IDS,
    TASK_IDS,
    synthetic_certificate as _synthetic_certificate,
)


@pytest.mark.parametrize("task_id", TASK_IDS)
@pytest.mark.parametrize("strategy_id", STRATEGY_IDS)
def test_gate_matches_frozen_strategy_matrix(
    task_id: str,
    strategy_id: str,
    tmp_path: Path,
) -> None:
    task = load_pilot_suite().task(task_id)
    strategy = task.oracle.strategies[strategy_id]
    assert strategy.fixture_variant is not None
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, strategy.fixture_variant, workspace)
    certificate = _synthetic_certificate(task, strategy.fixture_variant, workspace)

    decision = evaluate_provenance(task, certificate)
    comparison = compare_gate_decision_to_oracle(task, strategy_id, decision)

    assert decision.evidence_status == strategy.evidence_status
    assert decision.scientific_status == strategy.scientific_status
    assert decision.reason_code == strategy.reason_code
    assert len(decision.predicates) == 11
    assert comparison.matches
    assert comparison.mismatched_fields == ()


def test_oracle_comparison_reports_each_mismatched_decision_field(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    decision = evaluate_provenance(task, _synthetic_certificate(task, "primary", workspace))

    comparison = compare_gate_decision_to_oracle(task, "I6", decision)

    assert not comparison.matches
    assert comparison.mismatched_fields == (
        "evidence_status",
        "scientific_status",
        "reason_code",
    )
    assert comparison.expected_evidence_status == "INVALID"
    assert comparison.observed_evidence_status == "ADMISSIBLE"
    assert comparison.expected_scientific_status == "UNDETERMINED"
    assert comparison.observed_scientific_status == "SUPPORTS"
    assert comparison.expected_reason_code == "positive_control_failed"
    assert comparison.observed_reason_code == "required_pipeline"


def test_incomplete_trace_is_insufficient_even_when_observed_path_is_valid(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = _synthetic_certificate(task, "primary", workspace, complete=False)

    decision = evaluate_provenance(task, certificate)

    assert decision.evidence_status == "INSUFFICIENT"
    assert decision.scientific_status == "UNDETERMINED"
    assert decision.reason_code == "incomplete_monitor_trace"


def test_gate_decision_can_be_embedded_in_certificate(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_seeded_comparison")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = _synthetic_certificate(task, "primary", workspace)

    gated = gate_certificate(task, certificate)

    assert isinstance(gated.provenance_adjudication, ProvenanceGateDecision)
    assert gated.provenance_adjudication.evidence_status == "ADMISSIBLE"


def test_gate_rejects_initial_workspace_outside_frozen_manifest(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = _synthetic_certificate(task, "primary", workspace)
    capture = certificate.command_captures[0]
    before = WorkspaceDigestSnapshot(
        files={**capture.before.files, "injected-answer.txt": "1" * 64},
        complete=True,
    )
    certificate.command_captures = [capture.model_copy(update={"before": before})]

    decision = evaluate_provenance(task, certificate)

    assert decision.evidence_status == "INVALID"
    assert decision.reason_code == "scientific_files_changed"
    integrity = next(
        result for result in decision.predicates if result.predicate == "scientific_files_unchanged"
    )
    assert integrity.status == "VIOLATED"
    assert integrity.paths == ("injected-answer.txt",)
