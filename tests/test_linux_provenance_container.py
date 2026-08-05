from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "provenance.Dockerfile"
LAUNCHER = ROOT / "scripts" / "run_linux_provenance.sh"


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
