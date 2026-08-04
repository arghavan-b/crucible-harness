"""Self-check the two development-only provenance task fixtures.

This command verifies frozen workspace manifests, executes trusted fixture
variants, and checks their scientific outputs.  It does not run the provenance
monitor and must not be interpreted as a provenance-verification result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crucible.benchmarks.provenance import (  # noqa: E402
    PilotTaskError,
    load_pilot_suite,
    run_fixture_variant,
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
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        help="Fixture variant to check; repeatable (default: every trusted variant).",
    )
    args = parser.parse_args(argv)

    try:
        suite = load_pilot_suite(args.suite_root)
        requested_tasks = set(args.task)
        unknown_tasks = requested_tasks - set(suite.manifest.task_ids)
        if unknown_tasks:
            parser.error("unknown task(s): " + ", ".join(sorted(unknown_tasks)))

        checked = 0
        for task in suite.tasks:
            if requested_tasks and task.task_id not in requested_tasks:
                continue
            variants = tuple(args.variant) if args.variant else tuple(task.oracle.variants)
            unknown_variants = set(variants) - set(task.oracle.variants)
            if unknown_variants:
                parser.error(
                    f"{task.task_id} has no variant(s): " + ", ".join(sorted(unknown_variants))
                )
            for variant_id in variants:
                with tempfile.TemporaryDirectory(prefix=f"{task.task_id}_{variant_id}_") as temp:
                    execution = run_fixture_variant(task, variant_id, Path(temp) / "workspace")
                print(
                    json.dumps(
                        {
                            "task_id": execution.task_id,
                            "variant_id": execution.variant_id,
                            "control_passed": execution.check.control_passed,
                            "ungated_scientific_status": (
                                execution.check.ungated_scientific_status
                            ),
                            "metrics": execution.check.metrics,
                            "enforced_constraints": execution.enforced_constraints,
                            "unenforced_constraints": execution.unenforced_constraints,
                        },
                        sort_keys=True,
                    )
                )
                checked += 1
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
