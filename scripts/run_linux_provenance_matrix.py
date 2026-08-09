#!/usr/bin/env python3
"""Run frozen controlled-task strategies in isolated Linux provenance containers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from crucible.benchmarks.provenance import PilotTaskError, load_pilot_suite  # noqa: E402
from crucible.benchmarks.provenance_container import (  # noqa: E402
    ensure_linux_provenance_image,
    run_frozen_strategy_in_container,
)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-root", default=None, help="Override the bundled pilot root.")
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Task ID to run; repeatable (default: every pilot task).",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Frozen strategy ID to run; repeatable (default: every strategy).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Writes raw certificate, gate decision, and metrics JSON beneath TASK/.",
    )
    parser.add_argument(
        "--workspace-parent",
        default=None,
        help="Optional parent for short-lived clean workspaces.",
    )
    parser.add_argument(
        "--image",
        default=os.environ.get("CRUCIBLE_PROVENANCE_IMAGE", "crucible-provenance-linux:local"),
        help="Linux provenance image tag.",
    )
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the image first.")
    args = parser.parse_args(argv)

    try:
        suite = load_pilot_suite(args.suite_root)
        task_ids = tuple(args.task) if args.task else suite.manifest.task_ids
        strategy_ids = tuple(args.strategy) if args.strategy else suite.manifest.strategy_ids
        if len(set(task_ids)) != len(task_ids):
            raise PilotTaskError("task selection contains duplicate IDs")
        if len(set(strategy_ids)) != len(strategy_ids):
            raise PilotTaskError("strategy selection contains duplicate IDs")
        unknown_tasks = set(task_ids) - set(suite.manifest.task_ids)
        unknown_strategies = set(strategy_ids) - set(suite.manifest.strategy_ids)
        if unknown_tasks:
            raise PilotTaskError("unknown task(s): " + ", ".join(sorted(unknown_tasks)))
        if unknown_strategies:
            raise PilotTaskError("unknown strategy(s): " + ", ".join(sorted(unknown_strategies)))

        digest = ensure_linux_provenance_image(
            image=args.image,
            repo_root=REPO_ROOT,
            rebuild=args.rebuild,
        )
        output_root = Path(args.output_dir).resolve()
        workspace_parent = Path(args.workspace_parent).resolve() if args.workspace_parent else None
        executions = []
        for task_id in task_ids:
            task = suite.task(task_id)
            for strategy_id in strategy_ids:
                raw_certificate = output_root / task_id / f"{strategy_id}.raw.certificate.json"
                gate_decision = output_root / task_id / f"{strategy_id}.gate.json"
                metrics = output_root / task_id / f"{strategy_id}.metrics.json"
                execution = run_frozen_strategy_in_container(
                    task,
                    strategy_id,
                    container_digest=digest,
                    raw_certificate_path=raw_certificate,
                    gate_decision_path=gate_decision,
                    metrics_path=metrics,
                    workspace_parent=workspace_parent,
                )
                executions.append(execution)
                print(
                    json.dumps(
                        {
                            "task_id": execution.task_id,
                            "strategy_id": execution.strategy_id,
                            "variant_id": execution.variant_id,
                            "frozen_command": execution.frozen_command,
                            "collector": execution.raw_certificate.command_captures[0].collector,
                            "container_digest": execution.container_digest,
                            "raw_certificate": str(execution.raw_certificate_path),
                            "gate_decision": str(execution.gate_decision_path),
                            "metrics": str(execution.metrics_path),
                            "runtime_s": execution.metrics.runtime_s,
                            "trace_size_bytes": execution.metrics.trace_size_bytes,
                            "event_count": execution.metrics.event_count,
                            "gate_latency_s": execution.metrics.gate_latency_s,
                            "evidence_status": execution.gate_decision.evidence_status,
                            "scientific_status": execution.gate_decision.scientific_status,
                            "reason_code": execution.gate_decision.reason_code,
                            "oracle_match": execution.oracle_comparison.matches,
                            "oracle_mismatched_fields": (
                                execution.oracle_comparison.mismatched_fields
                            ),
                        },
                        sort_keys=True,
                    )
                )
        mismatches = [
            execution for execution in executions if not execution.oracle_comparison.matches
        ]
        print(
            f"captured, gated, and oracle-compared {len(executions)} "
            "frozen task/strategy execution(s)"
        )
        if mismatches:
            for execution in mismatches:
                comparison = execution.oracle_comparison
                print(
                    f"oracle mismatch: {execution.task_id}/{execution.strategy_id}: "
                    + ", ".join(comparison.mismatched_fields),
                    file=sys.stderr,
                )
            return 1
        return 0
    except FileNotFoundError as exc:
        print(f"linux-provenance-matrix: executable not found: {exc.filename}", file=sys.stderr)
        return 127
    except (OSError, PilotTaskError, ValueError) as exc:
        print(f"linux-provenance-matrix: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
