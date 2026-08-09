"""One-command capture path used inside the Linux provenance container."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from crucible.benchmarks.provenance_capture import capture_frozen_command
from crucible.runners.base import LocalSubprocessRunner


def test_capture_runs_exact_frozen_command_once_without_planning(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("frozen input\n", encoding="utf-8")
    command = (
        "{python}",
        "-c",
        "from pathlib import Path; "
        "Path('outputs').mkdir(); "
        "Path('outputs/result.txt').write_text(Path('input.txt').read_text())",
    )
    output = tmp_path / "certificate.json"

    result = capture_frozen_command(
        workspace,
        command,
        experiment_id="exp_frozen_test",
        output_path=output,
        timeout_s=10,
        container_digest="sha256:test-image",
        runner=LocalSubprocessRunner(),
        network_policy="none",
    )

    assert result.run.all_succeeded
    assert result.command == (sys.executable, *command[1:])
    assert shlex.split(result.submitted_command) == list(result.command)
    assert len(result.certificate.plan.steps) == 1
    assert result.certificate.plan.steps[0].step_id == "frozen_command"
    assert len(result.certificate.command_captures) == 1
    assert result.certificate.command_captures[0].submitted_command == result.submitted_command
    assert result.certificate.container_digest == "sha256:test-image"
    assert result.certificate.artifact_contents["outputs/result.txt"] == "frozen input\n"
    assert output.is_file()
