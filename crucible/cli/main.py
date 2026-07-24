"""Crucible CLI — the first product surface (design §15, §16).

CLI first: it forces the schemas to be right before any API or UI exists.

    crucible submit <repo_uri> [--paper URI] [--spec PATH]
    crucible replay <trace_id>
    crucible report <experiment_id>

The REST API (§16) mirrors these once the schemas settle.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Execution-reliability layer for autonomous computational science.")


@app.command()
def submit(
    repo_dir: str = typer.Argument(..., help="Path to a local repo to run."),
    paper: str | None = typer.Option(None, "--paper", help="Paper PDF to extract claims from (needs an LLM key)."),
    repo_uri: str | None = typer.Option(None, "--repo-uri", help="Canonical repo URI for provenance."),
    out: str | None = typer.Option(None, "--out", help="Write the reproducibility certificate JSON here."),
    isolation: str = typer.Option("subprocess", "--runner", help="subprocess | docker"),
    image: str | None = typer.Option(None, "--image", help="Docker base image (with --runner docker)."),
    gpus: str | None = typer.Option(None, "--gpus", help="GPU passthrough, e.g. 'all' (docker only)."),
    recover: bool = typer.Option(False, "--recover", help="Enable diagnosis + recovery (design §9)."),
) -> None:
    """Intake -> plan -> validate -> execute -> adjudicate -> verdict + certificate (design §22)."""
    from crucible.pipeline import run_pipeline
    from crucible.validation import PlanValidationError

    llm = None
    if paper:
        from crucible.intake import default_client

        try:
            llm = default_client()
        except RuntimeError as exc:
            typer.echo(f"submit: {exc}")
            raise typer.Exit(code=1) from None

    envmgr = runner = None
    if isolation == "docker":
        from crucible.envmgr.manager import DockerEnvironmentManager
        from crucible.runners.base import DockerExecRunner

        envmgr = DockerEnvironmentManager(base_image=image or "crucible/base:py3.12", gpus=gpus)
        runner = DockerExecRunner(envmgr)

    recovery = None
    if recover:
        from crucible.recovery import (
            CascadingDiagnoser,
            LLMDiagnoser,
            RecoveryEngine,
            RuleDiagnoser,
            seed_library,
        )

        diagnoser = RuleDiagnoser()
        client = llm
        if client is None:
            try:
                from crucible.intake import default_client

                client = default_client()
            except RuntimeError:
                client = None
        if client is not None:
            diagnoser = CascadingDiagnoser(RuleDiagnoser(), LLMDiagnoser(client))
            typer.echo("recovery: rules + LLM diagnoser")
        else:
            typer.echo("recovery: rules only (set an API key for the LLM diagnoser)")
        recovery = RecoveryEngine(seed_library(), diagnoser)

    try:
        result = run_pipeline(repo_dir, repo_uri=repo_uri, paper=paper, llm=llm,
                              envmgr=envmgr, runner=runner, recovery=recovery)
    except PlanValidationError as exc:
        typer.echo("plan rejected by validation:")
        for f in exc.record.blocking():
            typer.echo(f"  {f.gate} [{f.step_id}]: {f.message}")
        raise typer.Exit(code=1) from None

    typer.echo(f"experiment: {result.spec.experiment_id}")
    typer.echo("steps:")
    for r in result.run.step_results:
        mark = "ok " if r.state.value == "SUCCEEDED" else "FAIL"
        typer.echo(f"  [{mark}] {r.step_id}")
        for rep in r.repairs:
            tag = "SCIENTIFIC" if rep.scientific else "infra"
            typer.echo(f"        ↳ repair: {rep.playbook_id} ({rep.cause}, {tag})")
    v = result.verdict
    typer.echo(f"\nverdict: {v.status.value} (confidence {v.confidence:.2f})")
    if v.reason:
        typer.echo(f"  reason: {v.reason}")
    if v.evidence.result and v.evidence.result.conclusion:
        typer.echo(f"  {v.evidence.result.conclusion}")

    out = out or f"{result.spec.experiment_id}.certificate.json"
    from crucible.certificate import save_certificate

    save_certificate(result.certificate, out)
    typer.echo(f"\ncertificate -> {out}   (crucible replay {out})")
    raise typer.Exit(code=0 if v.status.value in {"SUCCESS", "RESULT_NEGATIVE"} else 1)


@app.command()
def replay(
    certificate: str = typer.Argument(..., help="Path to a reproducibility certificate (JSON)."),
) -> None:
    """Reproduce a run byte-comparably from its certificate (design §4.4, §6.5).

    Re-seeds the pinned source, re-runs the exact plan, and compares produced
    artifacts to the certificate manifest. Exit 0 iff reproduced.
    """
    from crucible.certificate import load_certificate, replay_certificate

    cert = load_certificate(certificate)
    report = replay_certificate(cert)
    typer.echo(f"experiment:   {report.experiment_id}")
    typer.echo(f"original run: {report.original_trace_id}")
    typer.echo(f"replay run:   {report.replay_trace_id}")
    if report.matched:
        typer.echo(f"byte-identical: {', '.join(report.matched)}")
    for j in report.expected_divergence:
        typer.echo(f"  expected divergence: {j.path} ({j.detail})")
    for j in report.unexpected_divergence:
        typer.echo(f"  UNEXPECTED divergence: {j.path} ({j.detail})")
    for path in report.missing:
        typer.echo(f"  MISSING: {path}")
    for j in report.unexpected_artifacts:
        typer.echo(f"  UNEXPECTED artifact: {j.path}")
    typer.echo(report.summary())
    raise typer.Exit(code=0 if report.reproduced else 1)


@app.command()
def intake(
    repo_dir: str = typer.Argument(..., help="Path to a local repo to analyze."),
    paper: str | None = typer.Option(None, "--paper", help="Path to a paper PDF to extract claims from."),
    repo_uri: str | None = typer.Option(None, "--repo-uri", help="Canonical repo URI (defaults to local path)."),
    out: str | None = typer.Option(None, "--out", help="Write the drafted ExperimentSpec JSON here."),
) -> None:
    """Draft an ExperimentSpec from a repo, optionally extracting claims from a paper (design §6.1).

    With --paper this parses the PDF (text/tables/figures) and uses an LLM
    (ANTHROPIC_API_KEY or OPENAI_API_KEY) to extract claims and baselines. Without
    it, a shallow repo-only spec is drafted offline.
    """
    from crucible.intake import Intake

    uri = repo_uri or f"local://{repo_dir}"

    if paper:
        from crucible.intake import default_client

        try:
            client = default_client()
        except RuntimeError as exc:
            typer.echo(f"intake: {exc}")
            raise typer.Exit(code=1) from None
        from crucible.intake import ground_claims

        spec, extraction, _analysis = Intake(llm=client).from_paper(paper, repo_uri=uri, root=repo_dir)
        typer.echo(f"title: {extraction.title or '(none)'}")
        typer.echo(f"extracted {len(extraction.claims)} claim(s), {len(extraction.baselines)} baseline(s)\n")
        bindings = {b.claim_id: b for b in ground_claims(extraction.claims, repo_dir, llm=client)}
        for c in extraction.claims:
            typer.echo(f"  claim {c.claim_id}: {c.comparison}  "
                       f"(reported {c.reported_value}, baseline {c.baseline_value})")
            typer.echo(f"     source: {c.source.location}   confidence: {c.confidence}")
            b = bindings.get(c.claim_id)
            if b:
                typer.echo(f"     reproduce: {b.run_command}   (grounding confidence {b.confidence})")
                if b.config_files:
                    typer.echo(f"     configs:   {', '.join(b.config_files)}")
    else:
        spec, _analysis = Intake().prepare(uri, root=repo_dir)
        typer.echo("no paper given — drafted a shallow repo-only spec (claims need a paper).\n")

    typer.echo(f"\nexperiment_id: {spec.experiment_id}")
    typer.echo(f"hypothesis:    {spec.hypothesis.type.value} — {spec.hypothesis.statement}")
    for claim in spec.claims_under_test:
        typer.echo(f"claim:         {claim.comparison}  reported={claim.reported_values} tol={claim.tolerance.value}")
    for pc in spec.positive_controls:
        typer.echo(f"control:       {pc.description} (expect {pc.expected} ± {pc.tolerance.value})")

    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(spec.model_dump_json(indent=2))
        typer.echo(f"\nwrote spec -> {out}")


@app.command()
def plan(repo_dir: str = typer.Argument(..., help="Path to a local repo to analyze and plan.")) -> None:
    """Intake -> plan -> validate for a local repo, printing the plan and gate result."""
    from crucible.intake import Intake
    from crucible.planner import PlannerError, TemplatePlanner
    from crucible.validation import validate

    spec, analysis = Intake().prepare(f"local://{repo_dir}", root=repo_dir)
    typer.echo(f"entry points: {analysis.entry_points or '(none detected)'}")
    typer.echo(f"manifests:    {analysis.dependency_manifests or '(none)'}")
    typer.echo(f"packages:     {analysis.top_level_packages or '(none)'}")
    try:
        execution_plan = TemplatePlanner().plan(spec, analysis)
    except PlannerError as exc:
        typer.echo(f"planner error: {exc}")
        raise typer.Exit(code=1) from None
    typer.echo("\nplan:")
    for step in execution_plan.steps:
        typer.echo(f"  {step.step_id:24s} {step.type.value:22s} verifier={step.verifier}")
    record = validate(execution_plan, spec)
    typer.echo(f"\nvalidation: {record.summary()}")
    raise typer.Exit(code=0 if record.passed else 1)


@app.command()
def bench(
    tasks_path: str | None = typer.Option(None, "--tasks", help="Adapted CORE-Bench JSON (default: bundled synthetic set)."),
    n: int | None = typer.Option(None, "--n", help="Stratified-sample size (default: all tasks)."),
) -> None:
    """Run the harness-on/off comparison and print the false-verdict table (design §12.3)."""
    from crucible.benchmarks import (
        HarnessOnArm,
        NaiveAgentArm,
        load_tasks,
        run_arm,
        stratified_sample,
    )
    from crucible.eval import render_table, run_comparison

    tasks = load_tasks(tasks_path)
    if n:
        tasks = stratified_sample(tasks, n)
    typer.echo(f"running {len(tasks)} task(s): harness-on (crucible) vs harness-off (bare-agent)\n")
    on = run_arm(HarnessOnArm(), tasks)
    off = run_arm(NaiveAgentArm(), tasks)
    rows = run_comparison(tasks, on, off)
    typer.echo(render_table(rows))
    overall = next(r for r in rows if r.stratum == "all")
    typer.echo(f"\nThe one number — false-verdict delta (off - on): {overall.false_verdict_delta:.0%}")


@app.command()
def report(experiment_id: str = typer.Argument(...)) -> None:
    """Print the verdict object and evidence chain for an experiment."""
    raise typer.Exit(code=_not_implemented("report"))


def _not_implemented(cmd: str) -> int:
    typer.echo(f"[crucible] '{cmd}' is scaffolded but not implemented yet (Stage 0).")
    return 1


if __name__ == "__main__":
    app()
