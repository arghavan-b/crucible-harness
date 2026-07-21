"""Docker environment path — integration tests, skipped when Docker is absent.

These run on a machine with a working `docker` daemon (e.g. Docker Desktop on a
Mac, CPU-only). They use the public `python:3.12-slim` image so no local base
image build is required.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="docker daemon not available")

IMAGE = "python:3.12-slim"


def test_docker_provision_exec_teardown() -> None:
    from crucible.envmgr.manager import DockerEnvironmentManager
    from crucible.runners.base import DockerExecRunner

    envmgr = DockerEnvironmentManager(base_image=IMAGE, network="none")
    runner = DockerExecRunner(envmgr)
    env = envmgr.provision()
    try:
        res = runner.run("echo hi && python3 -c 'print(2+2)'", working_dir=env.working_dir)
        assert res.exit_code == 0
        assert "hi" in res.stdout and "4" in res.stdout
    finally:
        envmgr.teardown(env)
    assert envmgr.container_for(env.working_dir) is None


def test_docker_pipeline_end_to_end(tmp_path) -> None:
    from crucible.envmgr.manager import DockerEnvironmentManager
    from crucible.pipeline import run_pipeline
    from crucible.runners.base import DockerExecRunner
    from crucible.schemas import VerdictStatus

    (tmp_path / "inference.py").write_text(
        "import json, os\n"
        "if __name__ == '__main__':\n"
        "    os.makedirs('outputs', exist_ok=True)\n"
        "    json.dump({'accuracy': 0.9}, open('outputs/metrics.json', 'w'))\n"
    )
    envmgr = DockerEnvironmentManager(base_image=IMAGE, network="none")
    runner = DockerExecRunner(envmgr)
    result = run_pipeline(str(tmp_path), envmgr=envmgr, runner=runner,
                          db_path=str(tmp_path / "trace.sqlite"))
    assert result.verdict.status is VerdictStatus.SUCCESS
