"""End-to-end Stage-0 slice on a synthetic local repo — no Docker, no network.

Demonstrates the full transactional loop: provision -> per-step (preconditions ->
snapshot -> execute -> capture ΔS -> verify -> commit) -> replayable trace.

The "repo" is a one-file image classifier stub that writes predictions.json.
Run with:  python -m examples.demo_local
"""

from __future__ import annotations

import os
import tempfile

import json

from crucible.adjudicator import Observations, adjudicate
from crucible.certificate import build_certificate, replay_certificate
from crucible.certificate.manifest import read_source
from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.executor.executor import RunResult, TransactionalExecutor
from crucible.runners.base import LocalSubprocessRunner
from crucible.schemas import (
    Action,
    ClaimUnderTest,
    ExecutionPlan,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    ReproducibilityCertificate,
    Source,
    Step,
    StepType,
    Tolerance,
    Verdict,
    VerdictStatus,
)
from crucible.trace.recorder import SQLiteTraceRecorder

INFERENCE_PY = '''\
import json
preds = [{"label": "cat", "score": 0.98}, {"label": "dog", "score": 0.02}]
with open("predictions.json", "w") as f:
    json.dump(preds, f)
print("wrote predictions.json with", len(preds), "predictions")
'''


def build_demo_spec() -> ExperimentSpec:
    return ExperimentSpec(
        experiment_id="exp_demo_local",
        hypothesis=Hypothesis(
            statement="The classifier repo runs and emits valid predictions.",
            type=HypothesisType.REPRODUCTION,
        ),
        source=Source(repo_uri="local://synthetic/classifier", commit="deadbeef"),
        claims_under_test=[
            ClaimUnderTest(
                claim_id="c1",
                metric="prediction_count",
                comparison="prediction_count > 0",
                reported_values={"prediction_count": 2.0},
                tolerance=Tolerance(value=0.0),
                seeds=[0],
            )
        ],
        positive_controls=[
            PositiveControl(
                control_id="pc1",
                description="predictions.json is produced and non-empty",
                metric="prediction_count",
                expected=2.0,
                tolerance=Tolerance(value=0.0),
            )
        ],
    )


def build_demo_plan() -> ExecutionPlan:
    return ExecutionPlan(
        experiment_id="exp_demo_local",
        steps=[
            Step(
                step_id="provision_dependencies_1",
                type=StepType.PROVISION_DEPENDENCIES,
                preconditions=['file_exists("inference.py")'],
                action=Action(kind="shell", command="python3 -c 'import json'"),
                postconditions=['imports_resolvable("json")'],
                verifier="imports_resolvable",
                verifier_args={"packages": ["json"], "python": "python3"},
            ),
            Step(
                step_id="full_run_1",
                type=StepType.FULL_RUN,
                preconditions=['file_exists("inference.py")', 'imports_resolvable("json")'],
                action=Action(kind="shell", command="python3 inference.py"),
                postconditions=['file_exists("predictions.json")'],
                verifier="exit_code_zero",
            ),
            Step(
                step_id="evaluate_claims_1",
                type=StepType.EVALUATE_CLAIMS,
                preconditions=['file_exists("predictions.json")'],
                action=Action(kind="shell", command="cat predictions.json"),
                postconditions=[],
                verifier="file_exists",
                verifier_args={"path": "predictions.json", "min_size": 2},
            ),
        ],
    )


def build_executor(db_path: str | None = None) -> tuple[TransactionalExecutor, ExecutionPlan]:
    envmgr = LocalEnvironmentManager()
    env = envmgr.provision()
    # Seed the synthetic repo into the workspace (stands in for acquire_source).
    with open(os.path.join(env.working_dir, "inference.py"), "w") as f:
        f.write(INFERENCE_PY)
    recorder = SQLiteTraceRecorder(db_path or os.path.join(tempfile.mkdtemp(), "trace.sqlite"))
    runner = LocalSubprocessRunner()
    executor = TransactionalExecutor(envmgr=envmgr, runner=runner, recorder=recorder, env=env)
    return executor, build_demo_plan()


def certify() -> ReproducibilityCertificate:
    """Run the demo and assemble a self-contained reproducibility certificate."""
    executor, plan = build_executor(db_path=os.path.join(tempfile.mkdtemp(), "demo.sqlite"))
    source_files = read_source(executor._env.working_dir)  # capture inputs BEFORE running
    result: RunResult = executor.execute(plan)

    # Extract the observed metric from the produced artifact and adjudicate.
    predictions_path = os.path.join(executor._env.working_dir, "predictions.json")
    count = len(json.load(open(predictions_path))) if os.path.exists(predictions_path) else 0
    observations = Observations(
        claim_series={"prediction_count": [float(count)]},
        control_values={"pc1": float(count)},
    )
    verdict = adjudicate(build_demo_spec(), result, "c1", observations)
    return build_certificate(
        spec=build_demo_spec(),
        plan=plan,
        run_result=result,
        working_dir=executor._env.working_dir,
        source_files=source_files,
        verdict=verdict,
    )


def main() -> None:
    executor, plan = build_executor(db_path=os.path.join(tempfile.mkdtemp(), "demo.sqlite"))
    result = executor.execute(plan)

    print(f"\nexperiment: {result.experiment_id}")
    print(f"trace_id:   {result.trace_id}")
    print(f"all_succeeded: {result.all_succeeded}   stopped_at: {result.stopped_at}\n")
    for r in result.step_results:
        mark = "✓" if r.state.value == "SUCCEEDED" else "✗"
        print(f"  {mark} {r.step_id:24s} {r.state.value:10s} verifier={r.verifier_detail}")

    print("\n--- trace events ---")
    for ev in executor.recorder.events(result.trace_id):  # type: ignore[attr-defined]
        print(f"  [{ev['kind']}] {ev['payload']}")

    # Reproducibility: certify this run, then replay it from the certificate.
    print("\n=== REPRODUCIBILITY ===")
    cert = certify()
    print(f"verdict:     {cert.verdict.status.value} "
          f"(confidence {cert.verdict.confidence:.2f}) — {cert.verdict.evidence.result.conclusion}")
    print(f"certificate: {len(cert.source_files)} source file(s), "
          f"{len(cert.artifact_manifest)} artifact(s) hashed")
    report = replay_certificate(cert)
    print(f"replay trace: {report.replay_trace_id}")
    print(report.summary())


if __name__ == "__main__":
    main()
