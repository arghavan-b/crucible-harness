"""Self-check a controlled provenance suite without evaluating its verifier.

This command verifies suite pins and workspace manifests, runs each selected
construction in a clean local workspace, and compares scientific artifacts with
the harness-owned construction oracle. It does not collect or gate provenance.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crucible.benchmarks.provenance import (  # noqa: E402
    DEFAULT_CONFIRMATORY_ROOT,
    ControlledSuiteError,
    load_controlled_suite,
    run_fixture_matrix,
)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=DEFAULT_CONFIRMATORY_ROOT,
        help="Controlled suite root (default: bundled confirmatory suite).",
    )
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Task ID to check; repeatable (default: every task).",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Strategy ID to check; repeatable (default: every strategy).",
    )
    parser.add_argument(
        "--workspace-parent",
        type=Path,
        default=None,
        help="Optional parent for short-lived construction workspaces.",
    )
    args = parser.parse_args(argv)

    try:
        suite = load_controlled_suite(args.suite_root)
        selected_tasks = tuple(args.task) if args.task else None
        selected_strategies = tuple(args.strategy) if args.strategy else None
        executions = run_fixture_matrix(
            suite,
            task_ids=selected_tasks,
            strategy_ids=selected_strategies,
            workspace_parent=args.workspace_parent,
        )
        for execution in executions:
            print(
                json.dumps(
                    {
                        "control_passed": execution.check.control_passed,
                        "metrics": execution.check.metrics,
                        "strategy_id": execution.strategy_id,
                        "task_id": execution.task_id,
                        "ungated_scientific_status": (
                            execution.check.ungated_scientific_status
                        ),
                        "variant_id": execution.variant_id,
                    },
                    sort_keys=True,
                )
            )
        print(
            f"verified {len(executions)} construction execution(s) for "
            f"{suite.manifest.suite_id}; no provenance decision was evaluated"
        )
        return 0
    except (OSError, ControlledSuiteError, ValueError) as exc:
        print(f"controlled-suite-check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
