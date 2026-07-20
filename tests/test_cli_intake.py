"""CLI `crucible intake` tests."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from crucible.cli.main import app

runner = CliRunner()


def _repo(tmp_path):
    (tmp_path / "train.py").write_text("if __name__ == '__main__':\n    pass\n")
    (tmp_path / "requirements.txt").write_text("numpy\n")
    return str(tmp_path)


def test_intake_repo_only(tmp_path) -> None:
    result = runner.invoke(app, ["intake", _repo(tmp_path)])
    assert result.exit_code == 0
    assert "repo-only spec" in result.stdout
    assert "experiment_id:" in result.stdout
    assert "control:" in result.stdout


def test_intake_writes_spec(tmp_path) -> None:
    out = str(tmp_path / "spec.json")
    result = runner.invoke(app, ["intake", _repo(tmp_path), "--out", out])
    assert result.exit_code == 0
    data = json.loads(open(out).read())
    assert data["experiment_id"].startswith("exp_")
    assert data["positive_controls"]


def test_intake_paper_without_key_errors_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "paper.pdf").write_bytes(b"%PDF-1.4 fake")
    result = runner.invoke(app, ["intake", _repo(tmp_path), "--paper", str(tmp_path / "paper.pdf")])
    assert result.exit_code == 1
    assert "No LLM API key" in result.stdout
