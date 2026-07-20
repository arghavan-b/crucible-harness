"""End-to-end test of the Stage-0 transactional slice (no Docker/network)."""

from __future__ import annotations

import os

from crucible.schemas import StepState
from examples.demo_local import build_executor


def test_full_run_succeeds_and_traces(tmp_path) -> None:
    executor, plan = build_executor(db_path=str(tmp_path / "trace.sqlite"))
    result = executor.execute(plan)

    assert result.all_succeeded
    assert result.stopped_at is None
    assert [r.state for r in result.step_results] == [StepState.SUCCEEDED] * 3

    # The artifact was actually produced in the workspace.
    workdir = executor._env.working_dir  # type: ignore[union-attr]
    assert os.path.exists(os.path.join(workdir, "predictions.json"))

    # Every run is recorded: commands, verifications, and a final state.
    kinds = [e["kind"] for e in executor.recorder.events(result.trace_id)]  # type: ignore[attr-defined]
    assert "run_started" in kinds
    assert kinds.count("command") == 3
    assert kinds.count("verification") == 3
    assert "run_finished" in kinds


def test_verifier_failure_stops_run(tmp_path) -> None:
    executor, plan = build_executor(db_path=str(tmp_path / "trace.sqlite"))
    # Break the run step: command exits non-zero, so exit_code_zero must fail.
    plan.steps[1].action.command = "python3 -c 'import sys; sys.exit(3)'"

    result = executor.execute(plan)

    assert not result.all_succeeded
    assert result.stopped_at == "full_run_1"
    # The third step must never run once the second fails (transactional stop).
    assert len(result.step_results) == 2
    assert result.step_results[1].state is StepState.FAILED
    assert "exit_code=3" in (result.step_results[1].verifier_detail or "")
