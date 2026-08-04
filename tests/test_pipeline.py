"""End-to-end submit pipeline tests (design §22)."""

from __future__ import annotations

import hashlib

import pytest

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

BROKEN_REPO = "import sys\nif __name__ == '__main__':\n    sys.exit(2)\n"


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
    assert result.certificate.command_captures == result.run.command_captures
    assert result.certificate.capture_summary == result.run.capture_summary
    assert result.certificate.provenance_adjudication == "not_performed"
    assert (
        result.certificate.pinned_inputs.dataset_checksums["inference.py"]
        == hashlib.sha256(GOOD_REPO.encode("utf-8")).hexdigest()
    )

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


def test_submit_recovers_a_broken_repo(tmp_path) -> None:
    # A repo whose entry imports a module that doesn't exist yet.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inference.py").write_text(
        "import helper_mod, json, os\n"
        "if __name__ == '__main__':\n"
        "    os.makedirs('outputs', exist_ok=True)\n"
        "    json.dump({'accuracy': helper_mod.VALUE}, open('outputs/metrics.json', 'w'))\n"
    )
    # Deterministic recovery: provide the missing module (stands in for pip install).
    from crucible.recovery import FailureCause, PlaybookLibrary, RecoveryEngine
    from crucible.recovery.playbook import Playbook, RepairAction

    lib = PlaybookLibrary(
        [
            Playbook(
                playbook_id="provide_helper",
                cause=FailureCause.MISSING_DEPENDENCY,
                repair=RepairAction(command="printf 'VALUE=0.9\\n' > helper_mod.py"),
            )
        ]
    )

    without = run_pipeline(str(repo), db_path=str(tmp_path / "a.sqlite"))
    assert without.verdict.status is VerdictStatus.EXECUTION_FAILURE  # fails without recovery

    withrec = run_pipeline(
        str(repo), db_path=str(tmp_path / "b.sqlite"), recovery=RecoveryEngine(lib)
    )
    assert withrec.run.all_succeeded  # recovery fixed it
    assert any(r.repairs for r in withrec.run.step_results)


def test_submit_certificate_has_real_plan(tmp_path) -> None:
    repo = _repo(tmp_path, GOOD_REPO)
    result = run_pipeline(repo, db_path=str(tmp_path / "t.sqlite"))
    step_types = [s.type.value for s in result.plan.steps]
    assert "smoke_run" in step_types and "full_run" in step_types
    assert step_types.index("positive_control_run") < step_types.index("evaluate_claims")


def test_binary_initial_input_fails_closed_on_text_only_replay(tmp_path) -> None:
    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("binary input validation must happen before execution")

    repo = tmp_path / "binary_repo"
    repo.mkdir()
    (repo / "data.bin").write_bytes(b"\xff\xfe\x00\x01")
    (repo / "inference.py").write_text(
        "import json, os\n"
        "payload = open('data.bin', 'rb').read()\n"
        "os.makedirs('outputs', exist_ok=True)\n"
        "json.dump({'bytes': len(payload)}, open('outputs/metrics.json', 'w'))\n",
        encoding="utf-8",
    )

    runner = CountingRunner()
    with pytest.raises(ValueError, match=r"non-replayable inputs: data\.bin"):
        run_pipeline(
            str(repo),
            db_path=str(tmp_path / "binary.sqlite"),
            runner=runner,  # type: ignore[arg-type]
        )

    assert runner.calls == 0
