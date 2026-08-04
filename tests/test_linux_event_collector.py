"""Linux strace parser and monitored-runner integration tests."""

from __future__ import annotations

import copy
import os
import shutil
import sys
from pathlib import Path

import pytest

from crucible.certificate import build_certificate, load_certificate, save_certificate
from crucible.certificate.manifest import file_manifest, read_source
from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.executor.executor import TransactionalExecutor
from crucible.runners import LinuxStraceRunner
from crucible.schemas import Action, ExecutionPlan, Step, StepType, Verdict, VerdictStatus
from crucible.trace import (
    CaptureFacet,
    CaptureState,
    MonitorContext,
    MonitoredCommandEnvelope,
    summarize_captures,
)
from crucible.trace.linux_strace import parse_strace_trace
from crucible.trace.recorder import SQLiteTraceRecorder
from examples.demo_local import build_demo_spec


def _context() -> MonitorContext:
    return MonitorContext(
        trace_id="trace_linux",
        experiment_id="experiment_linux",
        step_id="full_run",
        attempt=0,
    )


def test_parser_attributes_process_and_file_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prefix = tmp_path / "events"
    (tmp_path / "events.100").write_text(
        f'1710000000.000001 execve("/bin/sh", ["/bin/sh"], 0x0) = 0\n'
        f'1710000000.000002 openat(AT_FDCWD<{workspace}>, "input.txt", '
        f"O_RDONLY|O_CLOEXEC) = 3<{workspace}/input.txt>\n"
        f'1710000000.000003 read(3<{workspace}/input.txt>, "abc", 3) = 3\n'
        "1710000000.000004 clone(child_stack=NULL, flags=SIGCHLD) = 101\n"
        '1710000000.000007 rename("draft.txt", "result.txt") = 0\n'
        "1710000000.000008 +++ exited with 0 +++\n",
        encoding="utf-8",
    )
    (tmp_path / "events.101").write_text(
        '1710000000.000005 execve("/usr/bin/python3", ["python3"], 0x0) = 0\n'
        f'1710000000.000006 write(4<{workspace}/draft.txt>, "x", 1) = 1\n'
        "1710000000.000009 +++ exited with 0 +++\n",
        encoding="utf-8",
    )

    trace = parse_strace_trace(
        prefix,
        working_dir=str(workspace),
        strace_version="strace -- version 6.9",
    )

    assert trace.collection_complete
    assert trace.root_pid == 100
    assert trace.process_ids == (100, 101)
    assert any(
        event.operation == "spawn" and event.pid == 100 and event.child_pid == 101
        for event in trace.process_events
    )
    assert any(
        event.operation == "read"
        and event.pid == 100
        and event.workspace_path == "input.txt"
        and event.bytes_transferred == 3
        for event in trace.file_events
    )
    assert any(
        event.operation == "write" and event.pid == 101 and event.workspace_path == "draft.txt"
        for event in trace.file_events
    )
    rename = next(event for event in trace.file_events if event.operation == "rename")
    assert rename.workspace_path == "draft.txt"
    assert rename.target_workspace_path == "result.txt"
    assert set(trace.raw_trace_sha256) == {"pid:100", "pid:101"}
    with pytest.raises(TypeError):
        trace.raw_trace_sha256["pid:100"] = "0" * 64
    assert copy.deepcopy(trace) == trace


def test_parser_marks_unsupported_io_as_incomplete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prefix = tmp_path / "events"
    (tmp_path / "events.200").write_text(
        '1710000000.000001 execve("/bin/sh", ["/bin/sh"], 0x0) = 0\n'
        "1710000000.000002 io_uring_enter(3, 1, 0, 0, NULL, 0) = 1\n"
        "1710000000.000003 +++ exited with 0 +++\n",
        encoding="utf-8",
    )

    trace = parse_strace_trace(
        prefix,
        working_dir=str(workspace),
        strace_version="strace -- version 6.9",
    )

    assert not trace.collection_complete
    assert any("io_uring" in issue for issue in trace.issues)


def test_parser_reassembles_blocked_io_and_resolves_dirfd_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alternate = workspace / "nested"
    alternate.mkdir()
    prefix = tmp_path / "events"
    (tmp_path / "events.300").write_text(
        '1710000000.000001 execve("/bin/sh", ["/bin/sh"], 0x0) = 0\n'
        f'1710000000.000002 newfstatat(4<{alternate}>, "input.txt", '
        "{st_mode=S_IFREG|0644}, 0) = 0\n"
        f"1710000000.000003 read(3<{alternate}/input.txt>,  <unfinished ...>\n"
        "1710000000.000004 --- SIGCHLD {si_signo=SIGCHLD} ---\n"
        '1710000000.000005 <... read resumed>"abc", 3) = 3\n'
        '1710000000.000006 write(1<pipe:[123]>, "status", 6) = 6\n'
        f'1710000000.000007 mkdirat(4<{alternate}>, "created", 0755) = 0\n'
        f"1710000000.000008 fchmod(5<{alternate}/input.txt>, 0600) = 0\n"
        "1710000000.000009 +++ exited with 0 +++\n",
        encoding="utf-8",
    )

    trace = parse_strace_trace(
        prefix,
        working_dir=str(workspace),
        strace_version="strace -- version 6.9",
    )

    assert trace.collection_complete
    assert any(
        event.operation == "metadata_read" and event.workspace_path == "nested/input.txt"
        for event in trace.file_events
    )
    assert any(
        event.operation == "read"
        and event.workspace_path == "nested/input.txt"
        and event.bytes_transferred == 3
        for event in trace.file_events
    )
    assert not any(event.path.startswith("pipe:") for event in trace.file_events)
    assert any(
        event.operation == "namespace_write" and event.workspace_path == "nested/created"
        for event in trace.file_events
    )
    assert any(
        event.operation == "metadata_write" and event.workspace_path == "nested/input.txt"
        for event in trace.file_events
    )


def _fake_strace(tmp_path: Path) -> Path:
    executable = tmp_path / "strace"
    executable.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
import time

if "--version" in sys.argv:
    print("strace -- version fake-1")
    raise SystemExit(0)
if "--help" in sys.argv:
    print("-ff --output-separately -yy --decode-fds --kill-on-exit")
    raise SystemExit(0)

prefix = sys.argv[sys.argv.index("-o") + 1]
command = sys.argv[-1]
workspace = os.getcwd()
pid = 4242
stamp = time.time()
with open(f"{prefix}.{pid}", "w", encoding="utf-8") as trace:
    trace.write(f'{stamp:.6f} execve("/bin/sh", ["/bin/sh"], 0x0) = 0\\n')
    trace.write(
        f'{stamp + 0.000001:.6f} openat(AT_FDCWD<{workspace}>, "output.txt", '
        f'O_WRONLY|O_CREAT|O_TRUNC) = 3<{workspace}/output.txt>\\n'
    )
    trace.write(
        f'{stamp + 0.000002:.6f} write(3<{workspace}/output.txt>, "result", 6) = 6\\n'
    )
    trace.flush()

completed = subprocess.run(
    ["/bin/sh", "-lc", command], capture_output=True, text=True, cwd=workspace
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
with open(f"{prefix}.{pid}", "a", encoding="utf-8") as trace:
    trace.write(f"{stamp + 0.000003:.6f} +++ exited with {completed.returncode} +++\\n")
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_linux_runner_returns_complete_v2_envelope(tmp_path: Path) -> None:
    fake = _fake_strace(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = LinuxStraceRunner(str(fake), platform_name="linux")

    monitored = runner.run_monitored(
        "printf result > output.txt",
        str(workspace),
        _context(),
        timeout_s=5,
    )

    assert monitored.command is not None
    assert monitored.command.exit_code == 0
    capture = monitored.capture
    assert capture.schema_version == 2
    assert capture.collector == "crucible-linux-strace-v1"
    assert capture.scope == "linux_process_tree"
    assert capture.result.cleanup_status == "verified"
    assert capture.linux_events is not None
    assert capture.linux_events.collection_complete
    assert all(
        capture.completeness.facets[facet] is CaptureState.CAPTURED for facet in CaptureFacet
    )
    assert any(
        event.operation == "write" and event.workspace_path == "output.txt"
        for event in capture.linux_events.file_events
    )
    assert summarize_captures([capture], monitoring_requested=True).mode == "linux_events_v1"
    assert MonitoredCommandEnvelope.model_validate(capture.model_dump(mode="json")) == capture


def test_linux_events_survive_trace_and_certificate_roundtrip(tmp_path: Path) -> None:
    fake = _fake_strace(tmp_path)
    envmgr = LocalEnvironmentManager(base_dir=str(tmp_path / "environments"))
    env = envmgr.provision()
    (Path(env.working_dir) / "input.txt").write_text("declared", encoding="utf-8")
    source_files = read_source(env.working_dir)
    source_checksums = file_manifest(env.working_dir)
    recorder = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    plan = ExecutionPlan(
        experiment_id="exp_demo_local",
        steps=[
            Step(
                step_id="full_run",
                type=StepType.FULL_RUN,
                action=Action(kind="shell", command="printf result > output.txt"),
                verifier="exit_code_zero",
            )
        ],
    )
    run = TransactionalExecutor(
        envmgr=envmgr,
        runner=LinuxStraceRunner(str(fake), platform_name="linux"),
        recorder=recorder,
        env=env,
    ).execute(plan, validate=False)

    assert run.all_succeeded
    capture_event = next(
        event for event in recorder.events(run.trace_id) if event["kind"] == "command_capture"
    )
    assert capture_event["payload"]["linux_events"]["file_events"]
    certificate = build_certificate(
        spec=build_demo_spec(),
        plan=plan,
        run_result=run,
        working_dir=env.working_dir,
        source_files=source_files,
        source_checksums=source_checksums,
        verdict=Verdict(
            experiment_id=plan.experiment_id,
            claim_id="c1",
            status=VerdictStatus.SUCCESS,
        ),
    )
    certificate_path = str(tmp_path / "certificate.json")
    save_certificate(certificate, certificate_path)
    restored = load_certificate(certificate_path)

    assert restored.command_captures == run.command_captures
    assert restored.capture_summary is not None
    assert restored.capture_summary.mode == "linux_events_v1"
    assert restored.command_captures[0].linux_events is not None


def test_linux_runner_timeout_retains_partial_events(tmp_path: Path) -> None:
    fake = _fake_strace(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = LinuxStraceRunner(str(fake), platform_name="linux")

    monitored = runner.run_monitored("sleep 5", str(workspace), _context(), timeout_s=1)

    assert monitored.command is not None
    assert monitored.command.timed_out
    capture = monitored.capture
    assert capture.linux_events is not None
    assert not capture.linux_events.collection_complete
    assert capture.result.cleanup_status == "unverified"
    assert capture.completeness.facets[CaptureFacet.PROCESS_PARENTAGE] is CaptureState.INCOMPLETE
    assert summarize_captures([capture], monitoring_requested=True).mode == "partial"


def test_linux_runner_rejects_non_linux_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "must_not_exist"
    runner = LinuxStraceRunner(platform_name="darwin")

    with pytest.raises(RuntimeError, match="requires Linux"):
        runner.run_monitored(f"touch {marker}", str(tmp_path), _context())

    assert not marker.exists()


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("strace") is None,
    reason="requires Linux and a compatible strace",
)
def test_real_strace_smoke(tmp_path: Path) -> None:
    monitored = LinuxStraceRunner().run_monitored(
        "printf result > output.txt && cat output.txt",
        str(tmp_path),
        _context(),
        timeout_s=10,
    )

    assert monitored.command is not None
    assert monitored.command.exit_code == 0
    assert monitored.capture.linux_events is not None
    assert any(
        event.operation in {"open_write", "write"} and event.workspace_path == "output.txt"
        for event in monitored.capture.linux_events.file_events
    )


def test_fake_collector_is_executable(tmp_path: Path) -> None:
    fake = _fake_strace(tmp_path)
    assert os.access(fake, os.X_OK)
