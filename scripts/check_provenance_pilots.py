"""Self-check the two development-only provenance task fixtures.

This command verifies frozen workspace manifests, executes trusted fixture
strategies in isolated workspaces, and checks their scientific outputs. It does
not run the provenance monitor and must not be interpreted as a provenance-
verification result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crucible.benchmarks.provenance import (  # noqa: E402
    PilotTaskError,
    load_pilot_suite,
    run_fixture_matrix,
)


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite-root", default=None, help="Override the bundled pilot root.")
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        help="Task ID to check; repeatable (default: both tasks).",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--strategy",
        action="append",
        default=[],
        help="Strategy ID to check; repeatable (default: every frozen strategy).",
    )
    selection.add_argument(
        "--variant",
        action="append",
        default=[],
        help="Fixture variant to check; repeatable compatibility alias for --strategy.",
    )
    args = parser.parse_args(argv)

    try:
        suite = load_pilot_suite(args.suite_root)
        requested_tasks = set(args.task)
        unknown_tasks = requested_tasks - set(suite.manifest.task_ids)
        if unknown_tasks:
            parser.error("unknown task(s): " + ", ".join(sorted(unknown_tasks)))

        selected_task_ids = tuple(args.task) if args.task else None
        selected_strategy_ids: tuple[str, ...] | None = (
            tuple(args.strategy) if args.strategy else None
        )
        if args.variant:
            active_tasks = [
                task
                for task in suite.tasks
                if not requested_tasks or task.task_id in requested_tasks
            ]
            mapped: list[str] = []
            for variant_id in args.variant:
                matches = {
                    strategy_id
                    for task in active_tasks
                    for strategy_id, strategy in task.oracle.strategies.items()
                    if strategy.fixture_variant == variant_id
                }
                if len(matches) != 1:
                    parser.error(
                        f"fixture variant {variant_id!r} does not map to one frozen strategy"
                    )
                mapped.append(next(iter(matches)))
            selected_strategy_ids = tuple(mapped)

        executions = run_fixture_matrix(
            suite,
            task_ids=selected_task_ids,
            strategy_ids=selected_strategy_ids,
        )
        for execution in executions:
            print(
                json.dumps(
                    {
                        "task_id": execution.task_id,
                        "strategy_id": execution.strategy_id,
                        "variant_id": execution.variant_id,
                        "control_passed": execution.check.control_passed,
                        "ungated_scientific_status": (execution.check.ungated_scientific_status),
                        "metrics": execution.check.metrics,
                        "enforced_constraints": execution.enforced_constraints,
                        "unenforced_constraints": execution.unenforced_constraints,
                    },
                    sort_keys=True,
                )
            )
        checked = len(executions)
        print(
            f"verified {checked} fixture execution(s); no provenance claim was made; "
            "network isolation and the Linux monitor platform were not enforced"
        )
        return 0
    except (OSError, PilotTaskError, ValueError) as exc:
        print(f"provenance-pilot-check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
