"""Scientific-action command capture and executor-boundary tests."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.executor.executor import TransactionalExecutor
from crucible.runners.base import (
    CommandResult,
    LocalSubprocessRunner,
    MonitoredCommandResult,
    MonitoredRunner,
    Runner,
)
from crucible.schemas import Action, ExecutionPlan, PathClass, Step, StepState, StepType
from crucible.trace.capture import (
    CaptureFacet,
    CaptureState,
    MonitorContext,
    MonitoredCommandEnvelope,
)
from crucible.trace.recorder import SQLiteTraceRecorder


def _context(*, attempt: int = 0) -> MonitorContext:
    return MonitorContext(
        trace_id="trace_test",
        experiment_id="experiment_test",
        step_id="full_run",
        attempt=attempt,
    )


def _full_run_plan(
    *,
    command: str = "printf 'result' > output.txt",
    preconditions: list[str] | None = None,
    verifier: str = "exit_code_zero",
) -> ExecutionPlan:
    verifier_args: dict[str, object] = {}
    if verifier == "file_exists":
        verifier_args = {"path": "output.txt", "min_size": 1}
    return ExecutionPlan(
        experiment_id="experiment_test",
        steps=[
            Step(
                step_id="full_run",
                type=StepType.FULL_RUN,
                preconditions=preconditions or [],
                action=Action(kind="shell", command=command),
                verifier=verifier,
                verifier_args=verifier_args,
            )
        ],
    )


class SpyRunner:
    def __init__(self) -> None:
        self.delegate = LocalSubprocessRunner()
        self.ordinary_commands: list[str] = []
        self.monitored_commands: list[str] = []

    def monitoring_trust_basis(self) -> Literal["harness_tcb"]:
        return "harness_tcb"

    def run(
        self,
        command: str,
        working_dir: str,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> CommandResult:
        self.ordinary_commands.append(command)
        return self.delegate.run(command, working_dir, timeout_s, image)

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        self.monitored_commands.append(command)
        return self.delegate.run_monitored(command, working_dir, context, timeout_s, image)


class LegacyRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def monitoring_trust_basis(self) -> Literal["harness_tcb"]:
        return "harness_tcb"

    def run(
        self,
        command: str,
        working_dir: str,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> CommandResult:
        self.commands.append(command)
        return CommandResult(exit_code=0, stdout="", stderr="")


class RaisingRunner(LocalSubprocessRunner):
    def run(
        self,
        command: str,
        working_dir: str,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> CommandResult:
        raise OSError("runner unavailable")


class TimeoutWithStaleArtifactRunner(LocalSubprocessRunner):
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(
        self,
        command: str,
        working_dir: str,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> CommandResult:
        self.commands.append(command)
        if command.startswith("test -f"):
            return CommandResult(exit_code=0, stdout="5\n", stderr="")
        return CommandResult(
            exit_code=124,
            stdout="",
            stderr="timed out",
            timed_out=True,
        )


class RaisingMonitorRunner(LegacyRunner):
    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        raise RuntimeError("collector crashed")


class MutatingRaisingMonitorRunner(LegacyRunner):
    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        (Path(working_dir) / "side_effect.txt").write_text("uncertain", encoding="utf-8")
        raise RuntimeError("collector crashed after launch")


class MismatchedMonitorRunner(LegacyRunner):
    def __init__(self) -> None:
        super().__init__()
        self.delegate = LocalSubprocessRunner()

    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        return self.delegate.run_monitored("true", working_dir, context, timeout_s, image)


class MalformedMonitorRunner(LegacyRunner):
    def run_monitored(
        self,
        command: str,
        working_dir: str,
        context: MonitorContext,
        timeout_s: int = 1800,
        image: str | None = None,
    ) -> MonitoredCommandResult:
        return None  # type: ignore[return-value]


def test_local_monitored_path_captures_same_size_content_change(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("AAAA", encoding="utf-8")
    result = LocalSubprocessRunner().run_monitored(
        "printf 'BBBB' > value.txt",
        str(tmp_path),
        _context(),
    )

    capture = result.capture
    assert result.command is not None
    assert result.command.exit_code == 0
    assert capture.logical_working_dir == "."
    assert str(tmp_path) not in capture.model_dump_json()
    assert capture.envelope_duration_s >= capture.command_duration_s
    assert capture.before.files["value.txt"] != capture.after.files["value.txt"]
    assert target.stat().st_size == 4
    assert (
        capture.completeness.facets[CaptureFacet.PRE_POST_FILE_DIGESTS] is CaptureState.INCOMPLETE
    )
    assert capture.result.cleanup_status == "unverified"
    assert any("process-tree quiescence" in issue for issue in capture.completeness.issues)
    assert capture.completeness.facets[CaptureFacet.FILE_WRITE_EPISODES] is CaptureState.UNSUPPORTED


def test_same_content_rewrite_does_not_become_a_provenance_claim(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("same", encoding="utf-8")
    result = LocalSubprocessRunner().run_monitored(
        "printf 'same' > value.txt",
        str(tmp_path),
        _context(),
    )

    capture = result.capture
    assert capture.before.files == capture.after.files
    assert capture.completeness.facets[CaptureFacet.FILE_WRITE_EPISODES] is CaptureState.UNSUPPORTED
    assert capture.completeness.facets[CaptureFacet.FILE_READS] is CaptureState.UNSUPPORTED
    assert capture.completeness.facets[CaptureFacet.PROCESS_PARENTAGE] is CaptureState.UNSUPPORTED

    with pytest.raises(TypeError):
        capture.before.files["value.txt"] = "0" * 64
    with pytest.raises(TypeError):
        dict.__setitem__(capture.before.files, "forged.txt", "0" * 64)
    with pytest.raises(TypeError):
        capture.completeness.facets[CaptureFacet.FILE_READS] = CaptureState.CAPTURED
    assert copy.deepcopy(capture) == capture
    assert capture.model_copy(deep=True) == capture

    payload = capture.model_dump(mode="json")
    payload["completeness"]["facets"][CaptureFacet.FILE_READS.value] = CaptureState.CAPTURED.value
    with pytest.raises(ValidationError, match="cannot claim causal provenance"):
        MonitoredCommandEnvelope.model_validate(payload)


def test_symlink_makes_digest_snapshot_explicitly_incomplete(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("value", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    capture = (
        LocalSubprocessRunner()
        .run_monitored(
            "true",
            str(tmp_path),
            _context(),
        )
        .capture
    )

    assert not capture.before.complete
    assert not capture.after.complete
    assert (
        capture.completeness.facets[CaptureFacet.PRE_POST_FILE_DIGESTS] is CaptureState.INCOMPLETE
    )
    assert any("unsupported file entry link.txt" in issue for issue in capture.before.issues)


def test_executor_separates_scientific_action_from_harness_probes(tmp_path: Path) -> None:
    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "environments"))
    env = envmgr.provision()
    (Path(env.working_dir) / "input.txt").write_text("declared", encoding="utf-8")
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    runner = SpyRunner()
    executor = TransactionalExecutor(
        envmgr=envmgr,
        runner=runner,
        recorder=recorder,
        env=env,
    )

    run = executor.execute(
        _full_run_plan(
            preconditions=['file_exists("input.txt")'],
            verifier="file_exists",
        )
    )

    assert run.all_succeeded
    assert runner.monitored_commands == ["printf 'result' > output.txt"]
    assert len(runner.ordinary_commands) == 2
    assert all(command.startswith("test -f") for command in runner.ordinary_commands)
    step = run.step_results[0]
    assert len(step.command_captures) == 1
    assert run.command_captures == step.command_captures
    assert run.capture_summary.capture_count == 1
    assert run.capture_summary.mode == "partial"
    with pytest.raises(TypeError):
        run.capture_summary.facets[CaptureFacet.FILE_READS] = CaptureState.CAPTURED

    events = recorder.events(run.trace_id)
    kinds = [event["kind"] for event in events]
    assert kinds.count("command_capture") == 1
    assert kinds.index("command_capture") < kinds.index("command") < kinds.index("verification")
    command = next(event for event in events if event["kind"] == "command")
    assert command["payload"]["capture_id"] == step.command_captures[0].capture_id
    summary = next(event for event in events if event["kind"] == "capture_summary")
    assert summary["payload"] == run.capture_summary.model_dump(mode="json")


def test_legacy_runner_fails_closed_only_when_monitoring_is_requested(tmp_path: Path) -> None:
    legacy = LegacyRunner()
    assert isinstance(legacy, Runner)
    assert not isinstance(legacy, MonitoredRunner)

    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "required"))
    recorder = SQLiteTraceRecorder(str(tmp_path / "required.sqlite"))
    required = TransactionalExecutor(envmgr=envmgr, runner=legacy, recorder=recorder)
    failed = required.execute(_full_run_plan())

    assert not failed.all_succeeded
    assert failed.step_results[0].state is StepState.FAILED
    assert "requires a MonitoredRunner" in (failed.step_results[0].failure_reason or "")
    assert legacy.commands == []
    assert failed.capture_summary.mode == "unavailable"

    unmonitored_envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "optional"))
    unmonitored = TransactionalExecutor(
        envmgr=unmonitored_envmgr,
        runner=legacy,
        recorder=SQLiteTraceRecorder(str(tmp_path / "optional.sqlite")),
        monitor_scientific_actions=False,
    ).execute(_full_run_plan(command="true"))
    assert unmonitored.all_succeeded
    assert legacy.commands == ["true"]
    assert unmonitored.capture_summary.mode == "not_requested"


def test_precondition_failure_reports_no_scientific_action(tmp_path: Path) -> None:
    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "environments"))
    runner = SpyRunner()
    run = TransactionalExecutor(
        envmgr=envmgr,
        runner=runner,
        recorder=SQLiteTraceRecorder(str(tmp_path / "trace.sqlite")),
    ).execute(
        _full_run_plan(preconditions=['file_exists("missing.txt")']),
        validate=False,
    )

    assert not run.all_succeeded
    assert runner.monitored_commands == []
    assert len(runner.ordinary_commands) == 1
    assert run.capture_summary.mode == "no_action"
    assert all(state is CaptureState.NOT_REQUESTED for state in run.capture_summary.facets.values())


def test_non_scientific_harness_step_uses_ordinary_runner_path(tmp_path: Path) -> None:
    runner = SpyRunner()
    plan = ExecutionPlan(
        experiment_id="experiment_test",
        steps=[
            Step(
                step_id="collect",
                type=StepType.COLLECT_ARTIFACTS,
                action=Action(kind="shell", command="true"),
                verifier="exit_code_zero",
            )
        ],
    )
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=runner,
        recorder=SQLiteTraceRecorder(str(tmp_path / "trace.sqlite")),
    ).execute(plan)

    assert run.all_succeeded
    assert runner.ordinary_commands == ["true"]
    assert runner.monitored_commands == []
    assert run.capture_summary.mode == "not_requested"


def test_full_run_is_monitored_even_if_path_class_is_infrastructure(tmp_path: Path) -> None:
    runner = SpyRunner()
    plan = _full_run_plan(command="true")
    plan.steps[0].path_class = PathClass.INFRASTRUCTURE
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=runner,
        recorder=SQLiteTraceRecorder(str(tmp_path / "trace.sqlite")),
    ).execute(plan)

    assert run.all_succeeded
    assert runner.monitored_commands == ["true"]
    assert runner.ordinary_commands == []


def test_runner_error_capture_is_retained_on_failed_action(tmp_path: Path) -> None:
    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "environments"))
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    run = TransactionalExecutor(
        envmgr=envmgr,
        runner=RaisingRunner(),
        recorder=recorder,
    ).execute(_full_run_plan(command="never executed"))

    assert not run.all_succeeded
    assert len(run.command_captures) == 1
    capture = run.command_captures[0]
    assert capture.result.outcome == "runner_error"
    assert capture.result.runner_error == "OSError: runner unavailable"
    assert capture.result.cleanup_status == "unverified"
    assert capture.completeness.facets[CaptureFacet.COMMAND_RESULT] is CaptureState.INCOMPLETE
    assert (
        capture.completeness.facets[CaptureFacet.PRE_POST_FILE_DIGESTS] is CaptureState.INCOMPLETE
    )
    assert run.step_results[0].exit_code is None
    assert run.step_results[0].verifier_detail is not None
    assert run.step_results[0].verifier_detail.startswith("not run:")
    events = recorder.events(run.trace_id)
    assert any(event["kind"] == "command_capture" for event in events)
    command = next(event for event in events if event["kind"] == "command")
    assert command["payload"]["exit_code"] is None
    assert command["payload"]["outcome"] == "runner_error"
    assert events[-2]["kind"] == "capture_summary"
    assert events[-1]["kind"] == "run_finished"


def test_timeout_cannot_pass_using_a_stale_artifact(tmp_path: Path) -> None:
    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "environments"))
    env = envmgr.provision()
    (Path(env.working_dir) / "output.txt").write_text("stale", encoding="utf-8")
    runner = TimeoutWithStaleArtifactRunner()
    run = TransactionalExecutor(
        envmgr=envmgr,
        runner=runner,
        recorder=SQLiteTraceRecorder(str(tmp_path / "trace.sqlite")),
        env=env,
    ).execute(_full_run_plan(command="long scientific action", verifier="file_exists"))

    assert not run.all_succeeded
    assert runner.commands == ["long scientific action"]
    capture = run.command_captures[0]
    assert capture.result.outcome == "timed_out"
    assert capture.result.cleanup_status == "unverified"
    assert (
        capture.completeness.facets[CaptureFacet.PRE_POST_FILE_DIGESTS] is CaptureState.INCOMPLETE
    )
    assert run.step_results[0].verifier_passed is False
    assert run.capture_summary.mode == "partial"
    assert run.step_results[0].verifier_detail is not None
    assert run.step_results[0].verifier_detail.startswith("not run:")


def test_timeout_output_is_decoded_and_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd="scientific action",
            timeout=1,
            output=b"partial\xff",
            stderr=b"timeout detail",
        )

    monkeypatch.setattr("crucible.runners.base.subprocess.run", raise_timeout)
    monitored = LocalSubprocessRunner().run_monitored(
        "scientific action",
        str(tmp_path),
        _context(),
        timeout_s=1,
    )

    assert monitored.command is not None
    assert monitored.command.timed_out
    assert monitored.command.stdout == "partial\ufffd"
    assert monitored.command.stderr.startswith("timeout detail")
    assert monitored.capture.result.outcome == "timed_out"


def test_monitor_exception_fails_closed_and_finishes_trace(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=RaisingMonitorRunner(),
        recorder=recorder,
    ).execute(_full_run_plan(command="must not fall back"))

    assert not run.all_succeeded
    assert run.capture_summary.mode == "unavailable"
    for facet, state in run.capture_summary.facets.items():
        expected = (
            CaptureState.UNSUPPORTED
            if facet
            in {
                CaptureFacet.PROCESS_IDENTITIES,
                CaptureFacet.PROCESS_PARENTAGE,
                CaptureFacet.FILE_READS,
                CaptureFacet.FILE_WRITE_EPISODES,
                CaptureFacet.FILE_RENAMES,
            }
            else CaptureState.INCOMPLETE
        )
        assert state is expected
    events = recorder.events(run.trace_id)
    kinds = [event["kind"] for event in events]
    assert "monitor_failed" in kinds
    assert kinds[-2:] == ["capture_summary", "run_finished"]


def test_monitor_failure_retains_uncertain_state_delta(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=MutatingRaisingMonitorRunner(),
        recorder=recorder,
    ).execute(_full_run_plan(command="possibly launched"))

    assert not run.all_succeeded
    assert run.step_results[0].state_delta["files_created"] == ["side_effect.txt"]
    events = recorder.events(run.trace_id)
    command = next(event for event in events if event["kind"] == "command")
    assert command["payload"]["outcome"] == "monitor_incomplete"
    delta = next(event for event in events if event["kind"] == "state_delta")
    assert delta["payload"]["uncertain"] is True


def test_mismatched_monitor_envelope_is_not_accepted(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=MismatchedMonitorRunner(),
        recorder=recorder,
    ).execute(_full_run_plan(command="printf result > output.txt"))

    assert not run.all_succeeded
    assert run.command_captures == []
    assert run.capture_summary.mode == "unavailable"
    assert any(event["kind"] == "monitor_invalid" for event in recorder.events(run.trace_id))


def test_malformed_monitor_response_fails_with_terminal_trace(tmp_path: Path) -> None:
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    run = TransactionalExecutor(
        envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
        runner=MalformedMonitorRunner(),
        recorder=recorder,
    ).execute(_full_run_plan(command="must not fall back"))

    assert not run.all_succeeded
    assert run.capture_summary.mode == "unavailable"
    kinds = [event["kind"] for event in recorder.events(run.trace_id)]
    assert "monitor_invalid" in kinds
    assert kinds[-2:] == ["capture_summary", "run_finished"]


def test_execution_plan_rejects_duplicate_step_ids() -> None:
    step = _full_run_plan().steps[0]
    with pytest.raises(ValidationError, match="step_id values must be unique"):
        ExecutionPlan(experiment_id="experiment_test", steps=[step, step.model_copy(deep=True)])


def test_executor_rechecks_step_ids_after_mutation(tmp_path: Path) -> None:
    plan = _full_run_plan()
    plan.steps.append(plan.steps[0].model_copy(deep=True))
    runner = LegacyRunner()
    with pytest.raises(ValueError, match="step_id values must remain unique"):
        TransactionalExecutor(
            envmgr=LocalEnvironmentManager(base_dir=str(tmp_path / "environments")),
            runner=runner,
            recorder=SQLiteTraceRecorder(str(tmp_path / "trace.sqlite")),
        ).execute(plan)
    assert runner.commands == []
