"""Generic controlled-suite experiment manifest and append-only ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.benchmarks.provenance import (
    compare_gate_decision_to_oracle,
    load_pilot_suite,
    run_fixture_variant,
)
from crucible.benchmarks.provenance_container import (
    LinuxContainerExecution,
    ProvenanceRunMetrics,
)
from crucible.benchmarks.provenance_experiment import run_controlled_suite_experiment
from crucible.benchmarks.provenance_gate import evaluate_provenance
from crucible.benchmarks.provenance_ledger import (
    AppendOnlyExperimentLedger,
    ControlledCase,
    ControlledRunManifest,
    verify_experiment_ledger,
    write_run_manifest,
)

from synthetic_certificates import synthetic_certificate


def test_ledger_hash_chain_detects_record_tampering(tmp_path: Path) -> None:
    path = tmp_path / "experiment-ledger.jsonl"
    ledger = AppendOnlyExperimentLedger(path, run_id="run_test", suite_id="suite_test")
    ledger.append("suite_planned", details={"planned_case_count": 1})
    ledger.append("case_planned", task_id="task", strategy_id="V1")
    ledger.append("attempt_started", task_id="task", strategy_id="V1", attempt=1)
    ledger.append(
        "attempt_failed",
        task_id="task",
        strategy_id="V1",
        attempt=1,
        details={"error_type": "SyntheticFailure", "error_message": "expected"},
    )
    ledger.append("suite_completed", details={"failed_case_count": 1})

    records = verify_experiment_ledger(path)

    assert [record.sequence for record in records] == list(range(5))
    assert records[-1].previous_record_sha256 == records[-2].record_sha256
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[3])
    tampered["details"]["error_message"] = "rewritten"
    lines[3] = json.dumps(tampered, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_experiment_ledger(path)


def test_run_manifest_is_write_once(tmp_path: Path) -> None:
    manifest = ControlledRunManifest(
        run_id="run_test",
        suite_id="suite_test",
        suite_role="development",
        created_at="2026-08-09T00:00:00+00:00",
        suite_manifest_sha256="0" * 64,
        selected_cases=(ControlledCase(task_id="task", strategy_id="V1"),),
        image_reference="image:test",
        rebuild_requested=False,
        git_commit=None,
        git_dirty=True,
        host_platform="test",
        host_architecture="test",
        python_version="3.12",
    )
    destination = tmp_path / "run-manifest.json"

    write_run_manifest(manifest, destination)

    with pytest.raises(FileExistsError):
        write_run_manifest(manifest, destination)


def test_experiment_plans_all_cases_and_continues_after_failure(tmp_path: Path) -> None:
    suite = load_pilot_suite()
    calls: list[str] = []

    def resolve_image(**_kwargs) -> str:
        return "sha256:test-image"

    def execute(task, strategy_id, **kwargs):
        calls.append(strategy_id)
        if strategy_id == "V1":
            raise RuntimeError("synthetic first-case failure")
        strategy = task.oracle.strategies[strategy_id]
        assert strategy.fixture_variant is not None
        workspace = tmp_path / f"workspace-{strategy_id}"
        run_fixture_variant(task, strategy.fixture_variant, workspace)
        certificate = synthetic_certificate(task, strategy.fixture_variant, workspace)
        decision = evaluate_provenance(task, certificate)
        comparison = compare_gate_decision_to_oracle(task, strategy_id, decision)
        raw_path = Path(kwargs["raw_certificate_path"])
        gate_path = Path(kwargs["gate_decision_path"])
        metrics_path = Path(kwargs["metrics_path"])
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(certificate.model_dump_json(indent=2), encoding="utf-8")
        gate_path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
        metrics = ProvenanceRunMetrics(
            task_id=task.task_id,
            strategy_id=strategy_id,
            trace_id=certificate.trace_id,
            runtime_s=1.0,
            trace_size_bytes=17,
            event_count=1,
            gate_latency_s=0.01,
            normalized_trace_size_bytes=11,
            certificate_size_bytes=raw_path.stat().st_size,
            gate_decision_size_bytes=gate_path.stat().st_size,
            raw_trace_file_count=1,
            process_event_count=1,
            file_event_count=0,
        )
        metrics_path.write_text(metrics.model_dump_json(indent=2), encoding="utf-8")
        return LinuxContainerExecution(
            task_id=task.task_id,
            strategy_id=strategy_id,
            variant_id=strategy.fixture_variant,
            frozen_command=task.oracle.variants[strategy.fixture_variant].command,
            raw_certificate_path=raw_path,
            raw_certificate=certificate,
            gate_decision_path=gate_path,
            gate_decision=decision,
            oracle_comparison=comparison,
            metrics_path=metrics_path,
            metrics=metrics,
            container_digest=kwargs["container_digest"],
            stdout="",
            stderr="",
        )

    output = tmp_path / "retained"
    result = run_controlled_suite_experiment(
        suite,
        output_root=output,
        repo_root=Path(__file__).resolve().parents[1],
        image="test-image",
        task_ids=("pilot_weighted_mean",),
        strategy_ids=("V1", "V2"),
        run_id="controlled_test",
        image_resolver=resolve_image,
        strategy_executor=execute,
    )

    assert calls == ["V1", "V2"]
    assert result.exit_code == 2
    assert len(result.failures) == 1
    assert result.attempts[1].succeeded
    assert result.manifest_path.is_file()
    records = verify_experiment_ledger(result.ledger_path)
    event_types = [record.event_type for record in records]
    assert event_types[:3] == ["suite_planned", "case_planned", "case_planned"]
    assert event_types.count("attempt_started") == 2
    assert event_types.count("attempt_failed") == 1
    assert event_types.count("attempt_completed") == 1
    assert event_types[-1] == "suite_completed"
    completed = next(record for record in records if record.event_type == "attempt_completed")
    assert len(completed.artifacts) == 3
    assert completed.details["oracle_match"] is True


def test_image_setup_failure_retains_the_complete_intent_set(tmp_path: Path) -> None:
    suite = load_pilot_suite()

    def fail_image(**_kwargs) -> str:
        raise RuntimeError("synthetic image failure")

    result = run_controlled_suite_experiment(
        suite,
        output_root=tmp_path / "retained",
        repo_root=Path(__file__).resolve().parents[1],
        image="test-image",
        task_ids=("pilot_weighted_mean",),
        strategy_ids=("V1", "I1"),
        run_id="controlled_setup_failure",
        image_resolver=fail_image,
    )

    assert result.exit_code == 2
    assert result.attempts == ()
    assert result.suite_error_type == "RuntimeError"
    records = verify_experiment_ledger(result.ledger_path)
    assert [record.event_type for record in records] == [
        "suite_planned",
        "case_planned",
        "case_planned",
        "suite_failed",
    ]
    assert [(record.task_id, record.strategy_id) for record in records[1:3]] == [
        ("pilot_weighted_mean", "V1"),
        ("pilot_weighted_mean", "I1"),
    ]
