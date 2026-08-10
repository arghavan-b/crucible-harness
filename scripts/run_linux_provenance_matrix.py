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

from crucible.benchmarks.provenance import (  # noqa: E402
    PilotTaskError,
    load_controlled_suite,
    load_pilot_suite,
)
from crucible.benchmarks.provenance_experiment import (  # noqa: E402
    run_controlled_suite_experiment,
)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite-root",
        default=None,
        help="Run an integrity-pinned controlled suite instead of the bundled pilot.",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Task ID to run; repeatable (default: every controlled task).",
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
    parser.add_argument(
        "--run-id",
        default=None,
        help="Stable experiment run ID (default: generated UUID).",
    )
    parser.add_argument(
        "--supersedes-run-id",
        default=None,
        help="Optional prior run ID linked as a protocol-authorized rerun.",
    )
    args = parser.parse_args(argv)

    try:
        suite = (
            load_controlled_suite(args.suite_root)
            if args.suite_root is not None
            else load_pilot_suite()
        )
        result = run_controlled_suite_experiment(
            suite,
            output_root=args.output_dir,
            repo_root=REPO_ROOT,
            image=args.image,
            rebuild=args.rebuild,
            task_ids=tuple(args.task) if args.task else None,
            strategy_ids=tuple(args.strategy) if args.strategy else None,
            workspace_parent=args.workspace_parent,
            run_id=args.run_id,
            supersedes_run_id=args.supersedes_run_id,
        )
        for attempt in result.attempts:
            execution = attempt.execution
            if execution is not None:
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
            else:
                print(
                    json.dumps(
                        {
                            "task_id": attempt.case.task_id,
                            "strategy_id": attempt.case.strategy_id,
                            "attempt_failed": True,
                            "error_type": attempt.error_type,
                            "error_message": attempt.error_message,
                            "retained_artifacts": [
                                artifact.relative_path for artifact in attempt.artifacts
                            ],
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
        if result.suite_error_type is not None:
            print(
                f"suite setup failed: {result.suite_error_type}: "
                f"{result.suite_error_message}",
                file=sys.stderr,
            )
        print(
            f"planned {len(result.manifest.selected_cases)}, attempted "
            f"{len(result.attempts)}, completed "
            f"{sum(attempt.succeeded for attempt in result.attempts)}, failed "
            f"{len(result.failures)}, unattempted "
            f"{len(result.manifest.selected_cases) - len(result.attempts)}, "
            f"oracle mismatches {len(result.mismatches)}"
        )
        print(f"run manifest -> {result.manifest_path}")
        print(f"experiment ledger -> {result.ledger_path}")
        if result.mismatches:
            for attempt in result.mismatches:
                assert attempt.execution is not None
                execution = attempt.execution
                comparison = execution.oracle_comparison
                print(
                    f"oracle mismatch: {execution.task_id}/{execution.strategy_id}: "
                    + ", ".join(comparison.mismatched_fields),
                    file=sys.stderr,
                )
        return result.exit_code
    except FileNotFoundError as exc:
        print(f"linux-provenance-matrix: executable not found: {exc.filename}", file=sys.stderr)
        return 127
    except (OSError, PilotTaskError, ValueError) as exc:
        print(f"linux-provenance-matrix: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
