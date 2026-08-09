from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from crucible.benchmarks.provenance import PilotTaskError, load_pilot_suite
from crucible.benchmarks.provenance_capture import capture_frozen_command
from crucible.benchmarks.provenance_container import (
    ProvenanceRunMetrics,
    build_linux_capture_argv,
    run_frozen_strategy_in_container,
)
from crucible.certificate import load_certificate, save_certificate
from crucible.runners.base import LocalSubprocessRunner
from crucible.schemas.provenance import ProvenanceGateDecision
from crucible.trace.capture import (
    CAUSAL_CAPTURE_FACETS,
    CaptureState,
    LinuxEventTrace,
)


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "provenance.Dockerfile"
LAUNCHER = ROOT / "scripts" / "run_linux_provenance.sh"
MATRIX_LAUNCHER = ROOT / "scripts" / "run_linux_provenance_matrix.py"


def test_provenance_image_contains_linux_trace_runtime() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "FROM ubuntu:24.04" in dockerfile
    assert "strace" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert 'ENTRYPOINT ["crucible"]' in dockerfile


def test_provenance_launcher_exposes_required_trace_permissions() -> None:
    launcher = LAUNCHER.read_text()

    assert "--cap-add SYS_PTRACE" in launcher
    assert "--security-opt seccomp=unconfined" in launcher
    assert "--network none" in launcher
    assert "CRUCIBLE_NETWORK_POLICY=none" in launcher
    assert "--runner linux-strace" in launcher


def test_provenance_launcher_help_does_not_require_docker() -> None:
    result = subprocess.run(
        [str(LAUNCHER), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "TASK_REPO" in result.stdout
    assert "SYS_PTRACE" in result.stdout


def test_frozen_capture_mounts_only_workspace_and_output(tmp_path: Path) -> None:
    workspace = tmp_path / "clean workspace"
    output = tmp_path / "certificates"
    workspace.mkdir()
    frozen = (
        "{python}",
        "-c",
        "from pathlib import Path; Path('outputs/result.json').write_text('{}')",
    )

    argv = build_linux_capture_argv(
        image="crucible-provenance:test",
        workspace=workspace,
        certificate_directory=output,
        experiment_id="opaque-run-id",
        frozen_command=frozen,
        timeout_s=30,
        container_digest="sha256:image",
        user="123:456",
    )

    mounts = [argv[index + 1] for index, token in enumerate(argv) if token == "--mount"]
    assert mounts == [
        f"type=bind,source={workspace.resolve()},target=/experiment",
        f"type=bind,source={output.resolve()},target=/output",
    ]
    assert str(ROOT) not in " ".join(argv)
    assert "trusted/oracles.json" not in " ".join(argv)
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "123:456"
    assert argv[argv.index("--workdir") + 1] == "/experiment"
    assert json.loads(argv[argv.index("--command-json") + 1]) == list(frozen)
    image_index = argv.index("crucible-provenance:test")
    assert argv[image_index + 1] == "provenance-capture"
    assert argv[argv.index("--out") + 1] == "/output/certificate.json"


def test_matrix_launcher_help_does_not_require_docker() -> None:
    result = subprocess.run(
        [sys.executable, str(MATRIX_LAUNCHER), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--strategy" in result.stdout
    assert "--output-dir" in result.stdout


def test_container_execution_retains_raw_certificate_and_gate_decision(
    tmp_path: Path,
) -> None:
    task = load_pilot_suite().task("pilot_weighted_mean")
    variant = task.oracle.variants["primary"]
    template_workspace = tmp_path / "template-workspace"
    task.materialize(template_workspace)
    template_capture = capture_frozen_command(
        template_workspace,
        variant.command,
        experiment_id="exp_retention_template",
        output_path=tmp_path / "template.certificate.json",
        timeout_s=task.contract.runtime.timeout_s,
        container_digest="sha256:retention-test",
        runner=LocalSubprocessRunner(),
        network_policy="none",
    )
    local_envelope = template_capture.certificate.command_captures[0]
    facets = dict(local_envelope.completeness.facets)
    facets.update({facet: CaptureState.INCOMPLETE for facet in CAUSAL_CAPTURE_FACETS})
    envelope = local_envelope.model_copy(
        update={
            "schema_version": 2,
            "collector": "crucible-linux-strace-v1",
            "scope": "linux_process_tree",
            "network_policy": "none",
            "completeness": local_envelope.completeness.model_copy(
                update={"facets": facets, "issues": ("synthetic incomplete trace",)}
            ),
            "linux_events": LinuxEventTrace(
                strace_version="strace synthetic",
                syscall_filter=(),
                root_pid=123,
                process_ids=(123,),
                process_events=(),
                file_events=(),
                raw_trace_sha256={"pid:123": "0" * 64},
                raw_trace_size_bytes={"pid:123": 17},
                collection_complete=False,
                issues=("synthetic incomplete trace",),
            ),
        }
    )
    container_certificate = template_capture.certificate.model_copy(
        update={"command_captures": [envelope]}
    )
    run_calls = 0

    def fake_docker_run(argv, **kwargs):
        nonlocal run_calls
        run_calls += 1
        mounts = [argv[index + 1] for index, token in enumerate(argv) if token == "--mount"]
        output_mount = next(mount for mount in mounts if mount.endswith(",target=/output"))
        output_source = output_mount.split(",source=", 1)[1].rsplit(",target=", 1)[0]
        save_certificate(container_certificate, str(Path(output_source) / "certificate.json"))
        return subprocess.CompletedProcess(argv, 0, stdout="captured", stderr="")

    raw_path = tmp_path / "retained" / "V1.raw.certificate.json"
    decision_path = tmp_path / "retained" / "V1.gate.json"
    metrics_path = tmp_path / "retained" / "V1.metrics.json"
    workspace_parent = tmp_path / "workspaces"
    execution = run_frozen_strategy_in_container(
        task,
        "V1",
        container_digest="sha256:retention-test",
        raw_certificate_path=raw_path,
        gate_decision_path=decision_path,
        metrics_path=metrics_path,
        workspace_parent=workspace_parent,
        run_command=fake_docker_run,
    )

    retained_raw = load_certificate(str(raw_path))
    retained_decision = ProvenanceGateDecision.model_validate_json(decision_path.read_text())
    retained_metrics = ProvenanceRunMetrics.model_validate_json(metrics_path.read_text())
    assert retained_raw.provenance_adjudication == "not_performed"
    assert retained_raw == container_certificate
    assert execution.raw_certificate == retained_raw
    assert retained_decision == execution.gate_decision
    assert retained_decision.trace_id == retained_raw.trace_id
    assert not execution.oracle_comparison.matches
    assert execution.oracle_comparison.mismatched_fields
    assert execution.raw_certificate_path == raw_path
    assert execution.gate_decision_path == decision_path
    assert retained_metrics == execution.metrics
    assert retained_metrics.trace_id == retained_raw.trace_id
    assert retained_metrics.runtime_s == envelope.command_duration_s
    assert retained_metrics.trace_size_bytes == 17
    assert retained_metrics.event_count == 0
    assert retained_metrics.gate_latency_s >= 0
    assert execution.metrics_path == metrics_path
    assert list(workspace_parent.iterdir()) == []

    raw_bytes = raw_path.read_bytes()
    decision_bytes = decision_path.read_bytes()
    metrics_bytes = metrics_path.read_bytes()
    with pytest.raises(PilotTaskError, match="refusing to overwrite"):
        run_frozen_strategy_in_container(
            task,
            "V1",
            container_digest="sha256:retention-test",
            raw_certificate_path=raw_path,
            gate_decision_path=decision_path,
            metrics_path=metrics_path,
            workspace_parent=workspace_parent,
            run_command=fake_docker_run,
        )
    assert run_calls == 1
    assert raw_path.read_bytes() == raw_bytes
    assert decision_path.read_bytes() == decision_bytes
    assert metrics_path.read_bytes() == metrics_bytes
