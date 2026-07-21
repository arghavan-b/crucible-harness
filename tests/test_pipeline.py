"""End-to-end submit pipeline tests (design §22)."""

from __future__ import annotations

from crucible.certificate import replay_certificate
from crucible.pipeline import run_pipeline
from crucible.schemas import VerdictStatus

GOOD_REPO = (
    "import json, os\n"
    "if __name__ == '__main__':\n"
    "    os.makedirs('outputs', exist_ok=True)\n"
    "    json.dump({'accuracy': 0.91}, open('outputs/metrics.json', 'w'))\n"
    "    print('done')\n"
)

BROKEN_REPO = (
    "import sys\n"
    "if __name__ == '__main__':\n"
    "    sys.exit(2)\n"
)


def _repo(tmp_path, body: str):
    repo = tmp_path / "repo"  # keep the repo separate from the trace db
    repo.mkdir()
    (repo / "inference.py").write_text(body)  # 'inference.py' is a known entry name
    return str(repo)


def test_submit_success_and_reproducible(tmp_path) -> None:
    repo = _repo(tmp_path, GOOD_REPO)
    result = run_pipeline(repo, db_path=str(tmp_path / "t.sqlite"))

    assert result.run.all_succeeded
    assert result.verdict.status is VerdictStatus.SUCCESS
    # The certificate carries the validation record and a passing verdict.
    assert result.certificate.validation is not None
    assert result.certificate.validation.passed
    assert result.certificate.verdict.status is VerdictStatus.SUCCESS

    # And it actually replays.
    report = replay_certificate(result.certificate)
    assert report.reproduced


def test_submit_execution_failure(tmp_path) -> None:
    repo = _repo(tmp_path, BROKEN_REPO)
    result = run_pipeline(repo, db_path=str(tmp_path / "t.sqlite"))

    assert not result.run.all_succeeded
    assert result.verdict.status is VerdictStatus.EXECUTION_FAILURE
    # It stopped at the first run step that failed its verifier.
    assert result.run.stopped_at is not None


def test_submit_certificate_has_real_plan(tmp_path) -> None:
    repo = _repo(tmp_path, GOOD_REPO)
    result = run_pipeline(repo, db_path=str(tmp_path / "t.sqlite"))
    step_types = [s.type.value for s in result.plan.steps]
    assert "smoke_run" in step_types and "full_run" in step_types
    assert step_types.index("positive_control_run") < step_types.index("evaluate_claims")
