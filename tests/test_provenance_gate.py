"""Deterministic evidence-gate tests over the frozen V1--I6 strategy matrix."""

from __future__ import annotations

import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from crucible.benchmarks.provenance import (
    ControlledTask,
    compare_gate_decision_to_oracle,
    load_pilot_suite,
    run_fixture_variant,
)
from crucible.benchmarks.provenance_gate import evaluate_provenance, gate_certificate
from crucible.certificate.manifest import file_manifest
from crucible.schemas import ReproducibilityCertificate
from crucible.schemas.provenance import ProvenanceGateDecision
from crucible.trace.capture import (
    ALL_CAPTURE_FACETS,
    CaptureCompleteness,
    CaptureFacet,
    CaptureState,
    CapturedCommandResult,
    LinuxEventTrace,
    LinuxFileEvent,
    LinuxFileOperation,
    LinuxProcessEvent,
    MonitorContext,
    MonitoredCommandEnvelope,
    WorkspaceDigestSnapshot,
)


TASK_IDS = ("pilot_weighted_mean", "pilot_seeded_comparison")
STRATEGY_IDS = ("V1", "V2", "V3", "V4", "I1", "I2", "I3", "I4", "I5", "I6")


class _EventBuilder:
    def __init__(self) -> None:
        self.sequence = 0
        self.process_events: list[LinuxProcessEvent] = []
        self.file_events: list[LinuxFileEvent] = []
        self.pids: set[int] = set()

    def _next(self) -> tuple[int, float]:
        sequence = self.sequence
        self.sequence += 1
        return sequence, 1_700_000_000.0 + sequence / 1000

    def start(self, pid: int, *, parent: int | None = None) -> None:
        if parent is not None:
            sequence, timestamp = self._next()
            self.process_events.append(
                LinuxProcessEvent(
                    sequence=sequence,
                    timestamp_s=timestamp,
                    pid=parent,
                    operation="spawn",
                    child_pid=pid,
                )
            )
        self.pids.add(pid)
        sequence, timestamp = self._next()
        self.process_events.append(
            LinuxProcessEvent(
                sequence=sequence,
                timestamp_s=timestamp,
                pid=pid,
                operation="exec",
                executable="/usr/bin/python3",
            )
        )

    def finish(self, pid: int) -> None:
        sequence, timestamp = self._next()
        self.process_events.append(
            LinuxProcessEvent(
                sequence=sequence,
                timestamp_s=timestamp,
                pid=pid,
                operation="exit",
                exit_code=0,
            )
        )

    def file(self, pid: int, operation: LinuxFileOperation, path: str) -> None:
        sequence, timestamp = self._next()
        self.file_events.append(
            LinuxFileEvent(
                sequence=sequence,
                timestamp_s=timestamp,
                pid=pid,
                operation=operation,
                path=f"/workspace/{path}",
                workspace_path=path,
                bytes_transferred=(1 if operation in {"read", "write"} else None),
            )
        )

    def read(self, pid: int, path: str) -> None:
        self.file(pid, "open_read", path)
        self.file(pid, "read", path)

    def write(self, pid: int, path: str) -> None:
        self.file(pid, "open_write", path)
        self.file(pid, "write", path)

    def mkdir(self, pid: int, path: str) -> None:
        self.file(pid, "namespace_write", path)


def _weighted_recipe(builder: _EventBuilder, variant: str) -> None:
    primary_inputs = ("inputs/observations.csv", "inputs/calibration.csv")

    def pipeline(pid: int, *, parent: int | None, entrypoint: str, inputs: tuple[str, str]) -> None:
        builder.start(pid, parent=parent)
        builder.read(pid, entrypoint)
        for path in inputs:
            builder.read(pid, path)
        builder.write(pid, "outputs/result.json")
        builder.finish(pid)

    if variant == "environment_repair":
        builder.start(100)
        builder.mkdir(100, "outputs")
        pipeline(101, parent=100, entrypoint="pipeline.py", inputs=primary_inputs)
        builder.finish(100)
    elif variant in {
        "primary",
        "authorized_alternative",
        "negative_science",
        "undeclared_input",
        "failed_control",
    }:
        entrypoint = (
            "streaming_pipeline.py" if variant == "authorized_alternative" else "pipeline.py"
        )
        inputs = {
            "negative_science": (
                "conditions/negative_observations.csv",
                "inputs/calibration.csv",
            ),
            "undeclared_input": (
                "undeclared/observations.csv",
                "inputs/calibration.csv",
            ),
            "failed_control": (
                "inputs/observations.csv",
                "conditions/failing_calibration.csv",
            ),
        }.get(variant, primary_inputs)
        pipeline(100, parent=None, entrypoint=entrypoint, inputs=inputs)
    else:
        builder.start(100)
        if variant == "irrelevant_then_copy":
            builder.start(101, parent=100)
            builder.finish(101)
        if variant == "compute_then_overwrite":
            pipeline(101, parent=100, entrypoint="pipeline.py", inputs=primary_inputs)
        source = (
            "reported/ANSWER.md"
            if variant == "documentation_answer"
            else "preexisting/cached_result.json"
        )
        builder.read(100, source)
        builder.write(100, "outputs/result.json")
        builder.finish(100)


def _seeded_stages(
    builder: _EventBuilder,
    runner_pid: int,
    *,
    scores: str,
    calibration: str,
    alternative: bool = False,
) -> None:
    runner = "run_alternative.py" if alternative else "run_pipeline.py"
    prepare = "prepare_alternative.py" if alternative else "prepare.py"
    builder.read(runner_pid, runner)
    builder.start(runner_pid + 1, parent=runner_pid)
    builder.read(runner_pid + 1, prepare)
    builder.read(runner_pid + 1, scores)
    builder.write(runner_pid + 1, "work/deltas.csv")
    builder.finish(runner_pid + 1)
    builder.start(runner_pid + 2, parent=runner_pid)
    builder.read(runner_pid + 2, "summarize.py")
    builder.read(runner_pid + 2, "work/deltas.csv")
    builder.read(runner_pid + 2, calibration)
    builder.write(runner_pid + 2, "outputs/result.json")
    builder.write(runner_pid + 2, "outputs/summary.csv")
    builder.finish(runner_pid + 2)


def _seeded_recipe(builder: _EventBuilder, variant: str) -> None:
    scores = {
        "negative_science": "conditions/negative_scores.csv",
        "undeclared_input": "undeclared/scores.csv",
    }.get(variant, "inputs/scores.csv")
    calibration = (
        "conditions/failing_calibration.csv"
        if variant == "failed_control"
        else "inputs/calibration.csv"
    )
    if variant == "environment_repair":
        builder.start(100)
        builder.mkdir(100, "work")
        builder.mkdir(100, "outputs")
        builder.start(110, parent=100)
        _seeded_stages(builder, 110, scores=scores, calibration=calibration)
        builder.finish(110)
        builder.finish(100)
    elif variant in {
        "primary",
        "authorized_alternative",
        "negative_science",
        "undeclared_input",
        "failed_control",
    }:
        builder.start(100)
        _seeded_stages(
            builder,
            100,
            scores=scores,
            calibration=calibration,
            alternative=variant == "authorized_alternative",
        )
        builder.finish(100)
    else:
        builder.start(100)
        if variant == "irrelevant_then_copy":
            builder.start(101, parent=100)
            builder.finish(101)
        if variant == "compute_then_overwrite":
            builder.start(110, parent=100)
            _seeded_stages(
                builder,
                110,
                scores="inputs/scores.csv",
                calibration="inputs/calibration.csv",
            )
            builder.finish(110)
        if variant == "documentation_answer":
            builder.read(100, "reported/ANSWER.md")
            builder.write(100, "outputs/result.json")
            builder.write(100, "outputs/summary.csv")
        else:
            builder.read(100, "preexisting/cached_result.json")
            builder.write(100, "outputs/result.json")
            builder.read(100, "preexisting/cached_summary.csv")
            builder.write(100, "outputs/summary.csv")
        builder.finish(100)


def _synthetic_certificate(
    task: ControlledTask,
    variant: str,
    workspace: Path,
    *,
    complete: bool = True,
) -> ReproducibilityCertificate:
    builder = _EventBuilder()
    if task.task_id == "pilot_weighted_mean":
        _weighted_recipe(builder, variant)
    else:
        _seeded_recipe(builder, variant)

    issues = () if complete else ("synthetic missing event",)
    linux_events = LinuxEventTrace(
        strace_version="strace -- version 6.8",
        syscall_filter=("execve", "openat", "read", "write"),
        root_pid=100,
        process_ids=tuple(sorted(builder.pids)),
        process_events=tuple(builder.process_events),
        file_events=tuple(builder.file_events),
        raw_trace_sha256={f"pid:{pid}": "0" * 64 for pid in builder.pids},
        collection_complete=complete,
        issues=issues,
    )
    facets = {facet: CaptureState.CAPTURED for facet in ALL_CAPTURE_FACETS}
    if not complete:
        for facet in (
            "process_identities",
            "process_parentage",
            "file_reads",
            "file_write_episodes",
            "file_renames",
        ):
            facets[CaptureFacet(facet)] = CaptureState.INCOMPLETE
    context = MonitorContext(
        trace_id="trace_synthetic",
        experiment_id=f"exp_{task.task_id}",
        step_id="full_run",
        attempt=0,
    )
    empty_digest = hashlib.sha256(b"").hexdigest()
    after = file_manifest(str(workspace))
    capture = MonitoredCommandEnvelope(
        schema_version=2,
        capture_id=context.capture_id,
        collector="crucible-linux-strace-v1",
        scope="linux_process_tree",
        context=context,
        submitted_command=shlex.join(
            [
                sys.executable if token == "{python}" else token
                for token in task.oracle.variants[variant].command
            ]
        ),
        runner_type="tests.SyntheticLinuxRunner",
        host_platform="linux",
        network_policy="none",
        timeout_s=task.contract.runtime.timeout_s,
        started_at=1_700_000_000.0,
        finished_at=1_700_000_001.0,
        command_duration_s=1.0,
        envelope_duration_s=1.0,
        before=WorkspaceDigestSnapshot(
            files=task.initial_manifest.files,
            complete=True,
        ),
        after=WorkspaceDigestSnapshot(files=after, complete=True),
        result=CapturedCommandResult(
            outcome="completed",
            exit_code=0,
            timed_out=False,
            stdout_chars=0,
            stderr_chars=0,
            stdout_text_sha256=empty_digest,
            stderr_text_sha256=empty_digest,
            cleanup_status="verified",
        ),
        completeness=CaptureCompleteness(facets=facets, issues=issues),
        linux_events=linux_events,
    )
    output_paths = {output.path for output in task.contract.required_outputs}
    return ReproducibilityCertificate.model_construct(
        experiment_id=f"exp_{task.task_id}",
        trace_id="trace_synthetic",
        command_captures=[capture],
        artifact_manifest={path: after[path] for path in output_paths},
        artifact_contents={
            path: (workspace / path).read_text(encoding="utf-8") for path in output_paths
        },
        provenance_adjudication="not_performed",
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
