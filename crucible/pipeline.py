"""End-to-end submit pipeline (design §22 online inference flow).

Chains the whole harness for a local repo:

    intake -> (ground) -> plan -> validate -> execute -> observe -> adjudicate -> certificate

The repo is seeded into a fresh workspace (standing in for acquire_source), then
run transactionally. Observations for the verdict are extracted generically: the
positive control's smoke exit code, whether an output artifact was produced, and
a best-effort scan of produced JSON files for named metrics. Real per-claim
metric extraction (parsing a specific results file) is the next depth increment.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass

from crucible.adjudicator import Observations, adjudicate
from crucible.certificate import build_certificate
from crucible.certificate.manifest import read_source
from crucible.envmgr.manager import EnvironmentManager, LocalEnvironmentManager
from crucible.executor.executor import RunResult, TransactionalExecutor
from crucible.intake import Intake, ground_claims
from crucible.intake.intake import _experiment_id
from crucible.intake.llm import LLMClient, LoggingLLMClient
from crucible.planner import TemplatePlanner
from crucible.runners.base import LocalSubprocessRunner, Runner
from crucible.schemas import ExecutionPlan, ExperimentSpec, ReproducibilityCertificate, Verdict
from crucible.trace.recorder import SQLiteTraceRecorder, TraceRecorder


@dataclass
class PipelineResult:
    spec: ExperimentSpec
    plan: ExecutionPlan
    run: RunResult
    verdict: Verdict
    certificate: ReproducibilityCertificate


def _seed_repo(repo_dir: str, working_dir: str) -> None:
    for name in os.listdir(repo_dir):
        if name == ".git":
            continue
        src = os.path.join(repo_dir, name)
        dst = os.path.join(working_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _produced_files(working_dir: str, initial: set[str]) -> list[str]:
    out: list[str] = []
    for dp, _dirs, files in os.walk(working_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(dp, f), working_dir)
            if rel not in initial:
                out.append(rel)
    return out


def _find_metric(working_dir: str, produced: list[str], name: str) -> float | None:
    key = name.lower()
    for rel in produced:
        if not rel.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(working_dir, rel), encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for obj in ([data] if isinstance(data, dict) else []):
            for k, v in obj.items():
                if k.lower() == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                    return float(v)
            for v in obj.values():  # one level of nesting
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        if k2.lower() == key and isinstance(v2, (int, float)) and not isinstance(v2, bool):
                            return float(v2)
    return None


def observe(spec: ExperimentSpec, run: RunResult, working_dir: str, initial: set[str]) -> Observations:
    produced = _produced_files(working_dir, initial)
    controls: dict[str, float] = {}
    for pc in spec.positive_controls:
        if pc.metric == "smoke_exit_code":
            controls[pc.control_id] = 0.0 if run.all_succeeded else 1.0
        else:
            v = _find_metric(working_dir, produced, pc.metric)
            if v is not None:
                controls[pc.control_id] = v  # else unmeasured -> INCONCLUSIVE(control_not_measured)

    series: dict[str, list[float]] = {}
    for claim in spec.claims_under_test:
        for var in claim.reported_values or {claim.metric: None}:
            if var == "output_artifact_produced":
                series[var] = [1.0 if produced else 0.0]
            else:
                v = _find_metric(working_dir, produced, var) or _find_metric(working_dir, produced, claim.metric)
                if v is not None:
                    series[var] = [v]
    return Observations(claim_series=series, control_values=controls)


def run_pipeline(
    repo_dir: str,
    repo_uri: str | None = None,
    paper: str | None = None,
    llm: LLMClient | None = None,
    db_path: str | None = None,
    envmgr: EnvironmentManager | None = None,
    runner: Runner | None = None,
    recorder: TraceRecorder | None = None,
) -> PipelineResult:
    repo_dir = os.path.abspath(repo_dir)
    uri = repo_uri or f"local://{os.path.basename(repo_dir)}"

    # Open one trace that spans the whole experiment — intake/grounding LLM calls
    # and execution — so the certificate records every model call (design §6.5).
    recorder = recorder or SQLiteTraceRecorder(
        db_path or os.path.join(tempfile.mkdtemp(prefix="crucible_submit_"), "trace.sqlite")
    )
    trace_id = recorder.start(_experiment_id(uri))

    intake_llm = LoggingLLMClient(llm, recorder, trace_id, "intake") if llm is not None else None
    intake = Intake(llm=intake_llm)
    if paper and llm is not None:
        spec, extraction, analysis = intake.from_paper(paper, repo_uri=uri, root=repo_dir)
        ground_llm = LoggingLLMClient(llm, recorder, trace_id, "grounding")
        bindings = ground_claims(extraction.claims, repo_dir, llm=ground_llm)
    else:
        spec, analysis = intake.prepare(uri, root=repo_dir)
        bindings = ground_claims([], repo_dir)  # no claims to ground offline

    plan = TemplatePlanner().plan(spec, analysis, bindings=bindings or None)

    envmgr = envmgr or LocalEnvironmentManager()
    env = envmgr.provision()
    _seed_repo(repo_dir, env.working_dir)
    source_files = read_source(env.working_dir)

    runner = runner or LocalSubprocessRunner()
    executor = TransactionalExecutor(envmgr=envmgr, runner=runner, recorder=recorder, env=env)
    try:
        run = executor.execute(plan, spec, trace_id=trace_id)  # shares the trace above

        claim_id = spec.claims_under_test[0].claim_id if spec.claims_under_test else "c1"
        observations = observe(spec, run, env.working_dir, set(source_files))
        verdict = adjudicate(spec, run, claim_id, observations)

        certificate = build_certificate(
            spec=spec,
            plan=plan,
            run_result=run,
            working_dir=env.working_dir,
            source_files=source_files,
            verdict=verdict,
        )
        return PipelineResult(spec=spec, plan=plan, run=run, verdict=verdict, certificate=certificate)
    finally:
        teardown = getattr(envmgr, "teardown", None)
        if teardown is not None:
            try:
                teardown(env)
            except Exception:
                pass
