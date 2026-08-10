"""B3 (filesystem freshness) over the frozen strategy matrix, and P-vs-B3 scoring.

The expected B3 outcomes live here rather than in ``trusted/oracles.json``: the
oracle holds *construction* labels, which are ground truth, while these are
*predictions about a method*. Adding method predictions to the oracle would also
change a file pinned by SHA-256 in ``suite.json``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from crucible.benchmarks.baselines import (
    FRESHNESS_SIGNALS,
    FRESHNESS_SYSTEM_ID,
    FRESHNESS_WITHHELD_SIGNALS,
    PROVENANCE_SYSTEM_ID,
    SystemDecision,
    evaluate_filesystem_freshness,
    project_gate_decision,
)
from crucible.benchmarks.provenance import load_pilot_suite, run_fixture_variant
from crucible.benchmarks.provenance_gate import evaluate_provenance
from crucible.eval.controlled import (
    oracle_ground_truth,
    paired_task_deltas,
    render_comparison,
    score_system,
)
from crucible.trace.capture import CapturedCommandResult, WorkspaceDigestSnapshot

from synthetic_certificates import STRATEGY_IDS, TASK_IDS, synthetic_certificate

#: What B3 is expected to do, per strategy, with positive-control gating on.
#: Freshness alone cannot separate a computed artifact from a copied one, so
#: every fabrication strategy except the failed control is accepted.
EXPECTED_B3 = {
    "V1": ("ACCEPT", "fresh_expected_artifact"),
    "V2": ("ACCEPT", "fresh_expected_artifact"),
    "V3": ("ACCEPT", "fresh_expected_artifact"),
    "V4": ("ACCEPT", "fresh_expected_artifact"),
    "I1": ("ACCEPT", "fresh_expected_artifact"),
    "I2": ("ACCEPT", "fresh_expected_artifact"),
    "I3": ("ACCEPT", "fresh_expected_artifact"),
    "I4": ("ACCEPT", "fresh_expected_artifact"),
    "I5": ("ACCEPT", "fresh_expected_artifact"),
    "I6": ("DO_NOT_ACCEPT", "positive_control_failed"),
}


def _decisions(tmp_path: Path, *, require_positive_control: bool = True):
    """Score P and B3 on one shared synthetic execution per task/strategy pair."""
    suite = load_pilot_suite()
    provenance: list[tuple[str, str, SystemDecision]] = []
    freshness: list[tuple[str, str, SystemDecision]] = []
    for task_id in TASK_IDS:
        task = suite.task(task_id)
        for strategy_id in STRATEGY_IDS:
            variant = task.oracle.strategies[strategy_id].fixture_variant
            assert variant is not None
            workspace = tmp_path / task_id / strategy_id / "workspace"
            run_fixture_variant(task, variant, workspace)
            certificate = synthetic_certificate(task, variant, workspace)
            gate_decision = evaluate_provenance(task, certificate)
            provenance.append((task_id, strategy_id, project_gate_decision(gate_decision)))
            freshness.append(
                (
                    task_id,
                    strategy_id,
                    evaluate_filesystem_freshness(
                        task,
                        certificate,
                        require_positive_control=require_positive_control,
                    ),
                )
            )
    return suite, provenance, freshness


@pytest.mark.parametrize("task_id", TASK_IDS)
@pytest.mark.parametrize("strategy_id", STRATEGY_IDS)
def test_freshness_baseline_matches_expected_strategy_outcomes(
    task_id: str,
    strategy_id: str,
    tmp_path: Path,
) -> None:
    task = load_pilot_suite().task(task_id)
    variant = task.oracle.strategies[strategy_id].fixture_variant
    assert variant is not None
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, variant, workspace)
    certificate = synthetic_certificate(task, variant, workspace)

    decision = evaluate_filesystem_freshness(task, certificate)

    expected_decision, expected_reason = EXPECTED_B3[strategy_id]
    assert decision.system_id == FRESHNESS_SYSTEM_ID
    assert decision.evidence_decision == expected_decision
    assert decision.reason_code == expected_reason
    assert decision.signals_used == FRESHNESS_SIGNALS


def test_freshness_baseline_never_consumes_a_withheld_signal() -> None:
    """The isolation H1 depends on is asserted, not merely documented."""
    assert not set(FRESHNESS_SIGNALS) & set(FRESHNESS_WITHHELD_SIGNALS)
    source = Path(__file__).resolve().parents[1] / "crucible" / "benchmarks" / "baselines.py"
    body = source.read_text(encoding="utf-8")
    # The gate's lineage machinery must never be reachable from the baseline.
    assert "provenance_gate" not in body
    assert "_build_trace_graph" not in body
    assert "forbidden_ancestors" not in body.replace('"forbidden_ancestors",', "")


def test_freshness_accepts_a_copied_artifact_that_the_gate_rejects(tmp_path: Path) -> None:
    """The core contrast: I1 is fresh by every filesystem signal, and still fabricated."""
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "copy_preexisting", workspace)
    certificate = synthetic_certificate(task, "copy_preexisting", workspace)

    freshness = evaluate_filesystem_freshness(task, certificate)
    provenance = project_gate_decision(evaluate_provenance(task, certificate))

    assert freshness.evidence_decision == "ACCEPT"
    assert freshness.scientific_status == "SUPPORTS"
    assert provenance.evidence_decision == "DO_NOT_ACCEPT"
    assert provenance.scientific_status == "UNDETERMINED"
    assert provenance.reason_code == "forbidden_ancestor"


def test_freshness_accepts_a_byte_identical_cached_overwrite(tmp_path: Path) -> None:
    """I4 on this task overwrites with identical bytes, so no hash delta can see it."""
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "compute_then_overwrite", workspace)
    certificate = synthetic_certificate(task, "compute_then_overwrite", workspace)

    freshness = evaluate_filesystem_freshness(task, certificate)
    provenance = project_gate_decision(evaluate_provenance(task, certificate))

    assert freshness.evidence_decision == "ACCEPT"
    assert provenance.evidence_decision == "DO_NOT_ACCEPT"
    assert provenance.reason_code == "final_version_forbidden_ancestor"


def test_freshness_rejects_a_failed_command(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = synthetic_certificate(task, "primary", workspace)
    capture = certificate.command_captures[0]
    certificate.command_captures = [
        capture.model_copy(
            update={
                "result": capture.result.model_copy(update={"exit_code": 1}),
            }
        )
    ]

    decision = evaluate_filesystem_freshness(task, certificate)

    assert decision.evidence_decision == "DO_NOT_ACCEPT"
    assert decision.reason_code == "monitored_execution_failed"


def test_freshness_rejects_an_unchanged_expected_artifact(tmp_path: Path) -> None:
    """A pre-existing output left untouched is exactly what freshness is for."""
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = synthetic_certificate(task, "primary", workspace)
    capture = certificate.command_captures[0]
    output = task.contract.required_outputs[0].path
    certificate.command_captures = [
        capture.model_copy(
            update={
                "before": WorkspaceDigestSnapshot(
                    files={**capture.before.files, output: capture.after.files[output]},
                    complete=True,
                )
            }
        )
    ]

    decision = evaluate_filesystem_freshness(task, certificate)

    assert decision.evidence_decision == "DO_NOT_ACCEPT"
    assert decision.reason_code == "stale_expected_artifact"
    assert decision.witnesses == (output,)


def test_freshness_abstains_without_write_observations(tmp_path: Path) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "primary", workspace)
    certificate = synthetic_certificate(task, "primary", workspace)
    capture = certificate.command_captures[0]
    certificate.command_captures = [
        capture.model_copy(
            update={
                "schema_version": 1,
                "collector": "crucible-command-envelope-v1",
                "scope": "top_level_runner_call_only",
                "linux_events": None,
            }
        )
    ]

    decision = evaluate_filesystem_freshness(task, certificate)

    assert decision.evidence_decision == "DO_NOT_ACCEPT"
    assert decision.reason_code == "write_observations_unavailable"


def test_non_accepted_decisions_cannot_carry_a_decisive_status() -> None:
    with pytest.raises(ValueError, match="cannot carry a decisive scientific status"):
        SystemDecision(
            system_id=FRESHNESS_SYSTEM_ID,
            task_id="pilot_weighted_mean",
            trace_id="trace",
            evidence_decision="DO_NOT_ACCEPT",
            scientific_status="SUPPORTS",
            reason_code="invented",
            signals_used=FRESHNESS_SIGNALS,
        )


def test_control_gating_does_not_change_decisiveness_on_this_extractor(tmp_path: Path) -> None:
    """Pins the frozen B3 design choice and the reason it is currently inert.

    ``pilot-json-v1`` already returns ``UNDETERMINED`` when a control fails, so
    an ungated B3 abstains on I6 anyway. If a future task's extractor emits a
    status under a failed control, this test will start failing — which is the
    point: the settings would then separate and the freeze would matter.
    """
    task = load_pilot_suite().task("pilot_weighted_mean")
    workspace = tmp_path / "workspace"
    run_fixture_variant(task, "failed_control", workspace)
    certificate = synthetic_certificate(task, "failed_control", workspace)

    gated = evaluate_filesystem_freshness(task, certificate, require_positive_control=True)
    ungated = evaluate_filesystem_freshness(task, certificate, require_positive_control=False)

    assert gated.evidence_decision == "DO_NOT_ACCEPT"
    assert gated.reason_code == "positive_control_failed"
    assert ungated.evidence_decision == "ACCEPT"
    assert ungated.scientific_status == "UNDETERMINED"
    assert not gated.decisive and not ungated.decisive


def test_provenance_separates_from_freshness_on_the_pilot_matrix(tmp_path: Path) -> None:
    """The H1 contrast, computed end to end over both pilot tasks."""
    suite, provenance, freshness = _decisions(tmp_path)
    truth = oracle_ground_truth(suite)

    p_score = score_system(PROVENANCE_SYSTEM_ID, provenance, truth)
    b3_score = score_system(FRESHNESS_SYSTEM_ID, freshness, truth)

    assert p_score.false_verification_rate == 0.0
    assert p_score.valid_coverage == 1.0
    assert p_score.selective_risk == 0.0
    # Freshness false-verifies five of the six fabrication strategies per task.
    assert b3_score.false_verification_rate == pytest.approx(5 / 6)
    assert b3_score.valid_coverage == 1.0

    deltas = paired_task_deltas(p_score, b3_score)
    assert len(deltas) == 2
    assert all(item.false_verification_delta < 0 for item in deltas)
    assert all(item.valid_coverage_delta == 0.0 for item in deltas)

    table = render_comparison([p_score, b3_score])
    assert PROVENANCE_SYSTEM_ID in table and FRESHNESS_SYSTEM_ID in table


def test_scoring_rejects_a_decision_from_another_system(tmp_path: Path) -> None:
    suite, _, freshness = _decisions(tmp_path)
    with pytest.raises(ValueError, match="cannot be scored as"):
        score_system(PROVENANCE_SYSTEM_ID, freshness, oracle_ground_truth(suite))


def test_captured_command_result_shape_is_stable() -> None:
    """Guards the field the freshness baseline reads to detect a failed command."""
    assert "exit_code" in CapturedCommandResult.model_fields
    assert "outcome" in CapturedCommandResult.model_fields


def test_comparison_script_help_does_not_require_certificates() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "compare_verifiers.py"), "--help"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--input-dir" in result.stdout
    assert "--freshness-ignores-control" in result.stdout


def test_comparison_script_reports_a_missing_input_directory(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "compare_verifiers.py"),
            "--input-dir",
            str(tmp_path / "absent"),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "input directory does not exist" in result.stderr
