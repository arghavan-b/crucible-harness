"""Reproducibility replay tests (design §4.4, §6.5)."""

from __future__ import annotations

import os

from crucible.certificate import (
    build_certificate,
    load_certificate,
    replay_certificate,
    save_certificate,
)
from crucible.certificate.manifest import read_source
from crucible.schemas import Verdict, VerdictStatus
from examples.demo_local import build_demo_spec, build_executor


def _run_and_certify(tmp_path, plan_mutator=None):
    executor, plan = build_executor(db_path=str(tmp_path / "orig.sqlite"))
    if plan_mutator is not None:
        plan_mutator(plan)
    source = read_source(executor._env.working_dir)  # type: ignore[union-attr]
    result = executor.execute(plan)
    verdict = Verdict(
        experiment_id=plan.experiment_id,
        claim_id="c1",
        status=VerdictStatus.SUCCESS if result.all_succeeded else VerdictStatus.EXECUTION_FAILURE,
    )
    cert = build_certificate(
        spec=build_demo_spec(),
        plan=plan,
        run_result=result,
        working_dir=executor._env.working_dir,  # type: ignore[union-attr]
        source_files=source,
        verdict=verdict,
    )
    return cert


def test_deterministic_run_reproduces(tmp_path) -> None:
    cert = _run_and_certify(tmp_path)
    assert cert.artifact_manifest  # predictions.json was hashed

    report = replay_certificate(cert)
    assert report.reproduced
    assert "predictions.json" in report.matched
    assert not report.unexpected_divergence
    assert not report.missing
    assert not report.unexpected_artifacts


def test_certificate_roundtrips_to_disk(tmp_path) -> None:
    cert = _run_and_certify(tmp_path)
    path = str(tmp_path / "cert.json")
    save_certificate(cert, path)
    restored = load_certificate(path)
    assert restored == cert
    assert replay_certificate(restored).reproduced


def test_nondeterminism_is_reported(tmp_path) -> None:
    # Make the run write a nondeterministic artifact (random bytes each run).
    def inject(plan):
        plan.steps[1].action.command = (
            "python3 inference.py && python3 -c "
            "\"import os,random; open('nondet.txt','w').write(str(random.random()))\""
        )

    cert = _run_and_certify(tmp_path, plan_mutator=inject)
    # Certificate captured one specific random value; a fresh replay produces another.
    assert "nondet.txt" in cert.artifact_manifest

    report = replay_certificate(cert)  # empty policy => strict
    assert not report.reproduced
    assert "nondet.txt" in [j.path for j in report.unexpected_divergence]
    # The deterministic artifact still matches — divergence is isolated.
    assert "predictions.json" in report.matched
