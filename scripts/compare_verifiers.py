#!/usr/bin/env python3
"""Score P against B3 over retained controlled-task certificates (protocol §9, §11).

Consumes the artifacts written by ``scripts/run_linux_provenance_matrix.py``:

    <input-dir>/<task_id>/<strategy_id>.raw.certificate.json   (required)
    <input-dir>/<task_id>/<strategy_id>.gate.json              (preferred for P)

Both systems are scored on the *same* captured execution, which is the point:
adding a comparator costs no extra runs and cannot perturb the trace. P is read
from the retained gate decision when one exists, so the analysis consumes the
artifact of record rather than silently recomputing it.

This reports point estimates only. The task-cluster bootstrap, exact permutation
test, and Wilson intervals of §12 are not implemented; no confirmatory claim
should be made from this output alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from crucible.benchmarks.baselines import (  # noqa: E402
    FRESHNESS_SYSTEM_ID,
    PROVENANCE_SYSTEM_ID,
    SystemDecision,
    evaluate_filesystem_freshness,
    project_gate_decision,
)
from crucible.benchmarks.provenance import (  # noqa: E402
    PilotTaskError,
    load_controlled_suite,
    load_pilot_suite,
)
from crucible.benchmarks.provenance_gate import evaluate_provenance  # noqa: E402
from crucible.certificate import load_certificate  # noqa: E402
from crucible.eval.controlled import (  # noqa: E402
    oracle_ground_truth,
    paired_task_deltas,
    render_comparison,
    score_system,
)
from crucible.schemas.provenance import ProvenanceGateDecision  # noqa: E402


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite-root",
        default=None,
        help="Score an integrity-pinned controlled suite instead of the bundled pilot.",
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Directory of retained certificates, laid out as TASK/STRATEGY.raw.certificate.json.",
    )
    parser.add_argument("--out", default=None, help="Write the scored summary JSON here.")
    parser.add_argument(
        "--freshness-ignores-control",
        action="store_true",
        help=(
            "Score B3 without positive-control gating. This is a frozen protocol choice, "
            "not a post-hoc option: see crucible/benchmarks/baselines.py."
        ),
    )
    args = parser.parse_args(argv)

    try:
        suite = (
            load_controlled_suite(args.suite_root)
            if args.suite_root is not None
            else load_pilot_suite()
        )
        ground_truth = oracle_ground_truth(suite)
        root = Path(args.input_dir).resolve()
        if not root.is_dir():
            raise PilotTaskError(f"input directory does not exist: {root}")

        provenance: list[tuple[str, str, SystemDecision]] = []
        freshness: list[tuple[str, str, SystemDecision]] = []
        records: list[dict[str, object]] = []
        recomputed: list[str] = []

        for task_id in suite.manifest.task_ids:
            task = suite.task(task_id)
            for strategy_id in suite.manifest.strategy_ids:
                certificate_path = root / task_id / f"{strategy_id}.raw.certificate.json"
                if not certificate_path.is_file():
                    continue
                certificate = load_certificate(str(certificate_path))

                decision_path = root / task_id / f"{strategy_id}.gate.json"
                if decision_path.is_file():
                    gate_decision = ProvenanceGateDecision.model_validate_json(
                        decision_path.read_text(encoding="utf-8")
                    )
                else:
                    gate_decision = evaluate_provenance(task, certificate)
                    recomputed.append(f"{task_id}/{strategy_id}")

                p_decision = project_gate_decision(gate_decision)
                b3_decision = evaluate_filesystem_freshness(
                    task,
                    certificate,
                    require_positive_control=not args.freshness_ignores_control,
                )
                provenance.append((task_id, strategy_id, p_decision))
                freshness.append((task_id, strategy_id, b3_decision))

                truth = ground_truth[(task_id, strategy_id)]
                record = {
                    "task_id": task_id,
                    "strategy_id": strategy_id,
                    "process_valid": truth.process_valid,
                    "expected_scientific_status": truth.expected_scientific_status,
                    PROVENANCE_SYSTEM_ID: {
                        "evidence_decision": p_decision.evidence_decision,
                        "scientific_status": p_decision.scientific_status,
                        "reason_code": p_decision.reason_code,
                    },
                    FRESHNESS_SYSTEM_ID: {
                        "evidence_decision": b3_decision.evidence_decision,
                        "scientific_status": b3_decision.scientific_status,
                        "reason_code": b3_decision.reason_code,
                    },
                }
                records.append(record)
                print(json.dumps(record, sort_keys=True))

        if not records:
            raise PilotTaskError(f"no retained certificates found under {root}")

        p_score = score_system(PROVENANCE_SYSTEM_ID, provenance, ground_truth)
        b3_score = score_system(FRESHNESS_SYSTEM_ID, freshness, ground_truth)
        deltas = paired_task_deltas(p_score, b3_score)

        print()
        print(render_comparison([p_score, b3_score]))
        if recomputed:
            print(
                f"\nnote: recomputed P for {len(recomputed)} execution(s) with no retained "
                f"gate decision: {', '.join(recomputed)}",
                file=sys.stderr,
            )
        print(
            "\npoint estimates only; §12 intervals and tests are not implemented",
            file=sys.stderr,
        )

        if args.out:
            summary = {
                "schema_version": 1,
                "executions": records,
                "systems": {
                    score.system_id: {
                        "false_verification_rate_macro": score.false_verification_rate,
                        "valid_coverage_macro": score.valid_coverage,
                        "false_verification_rate_pooled": score.pooled_false_verification_rate,
                        "valid_coverage_pooled": score.pooled_valid_coverage,
                        "selective_risk": score.selective_risk,
                        "decisiveness": score.decisiveness,
                        "tasks": [
                            {
                                "task_id": task.task_id,
                                "false_verification_rate": task.false_verification_rate,
                                "valid_coverage": task.valid_coverage,
                            }
                            for task in score.tasks
                        ],
                    }
                    for score in (p_score, b3_score)
                },
                "paired_task_deltas": [
                    {
                        "task_id": item.task_id,
                        "false_verification_delta": item.false_verification_delta,
                        "valid_coverage_delta": item.valid_coverage_delta,
                    }
                    for item in deltas
                ],
                "freshness_requires_positive_control": not args.freshness_ignores_control,
                "inferential_statistics_implemented": False,
            }
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(
                json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
            )
            print(f"summary -> {args.out}")
        return 0
    except (OSError, PilotTaskError, ValueError) as exc:
        print(f"compare-verifiers: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
