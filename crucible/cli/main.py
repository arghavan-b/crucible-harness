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
    repo_uri: str = typer.Argument(..., help="Repo URL, or path to a spec with --spec."),
    paper: str | None = typer.Option(None, help="Optional paper URI for intake."),
    spec: str | None = typer.Option(None, help="Path to an ExperimentSpec JSON to skip intake."),
) -> None:
    """Intake -> plan -> validate -> execute -> adjudicate -> verdict + certificate."""
    raise typer.Exit(code=_not_implemented("submit"))


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
def report(experiment_id: str = typer.Argument(...)) -> None:
    """Print the verdict object and evidence chain for an experiment."""
    raise typer.Exit(code=_not_implemented("report"))


def _not_implemented(cmd: str) -> int:
    typer.echo(f"[crucible] '{cmd}' is scaffolded but not implemented yet (Stage 0).")
    return 1


if __name__ == "__main__":
    app()
