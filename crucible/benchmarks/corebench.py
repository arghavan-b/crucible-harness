"""Benchmark task model + loader (design §12.2).

A BenchTask is a repository plus its known ground truth. CORE-Bench (270 tasks,
90 papers) is the intended real source; that data needs a download, so the
bundled `synthetic_tasks()` gives a small offline set with mixed difficulty and a
mix of reproducible and broken repos — enough to exercise the scoring rig and the
harness-on/off comparison without network.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

Difficulty = str  # "easy" | "medium" | "hard"


@dataclass
class GroundTruth:
    reproduces: bool                       # does the claim genuinely hold when run correctly?
    expected: dict[str, float] = field(default_factory=dict)
    notes: str | None = None


@dataclass
class BenchTask:
    task_id: str
    difficulty: Difficulty
    ground_truth: GroundTruth
    repo_files: dict[str, str] = field(default_factory=dict)  # relative path -> content

    def materialize(self, dest: str) -> str:
        for rel, content in self.repo_files.items():
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path) or dest, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return dest


# --- synthetic offline task set ----------------------------------------------

_GOOD = (
    "import json, os\n"
    "if __name__ == '__main__':\n"
    "    os.makedirs('outputs', exist_ok=True)\n"
    "    json.dump({{'accuracy': {acc}}}, open('outputs/metrics.json', 'w'))\n"
    "    print('done')\n"
)

_BROKEN_EXIT = (
    "import sys\n"
    "if __name__ == '__main__':\n"
    "    print('boom'); sys.exit(2)\n"
)

_BROKEN_IMPORT = (
    "import definitely_not_a_real_pkg_xyz\n"
    "if __name__ == '__main__':\n"
    "    pass\n"
)


def synthetic_tasks() -> list[BenchTask]:
    def good(tid: str, diff: str, acc: float) -> BenchTask:
        return BenchTask(
            task_id=tid, difficulty=diff,
            ground_truth=GroundTruth(reproduces=True, expected={"accuracy": acc}),
            repo_files={"inference.py": _GOOD.format(acc=acc)},
        )

    def broken(tid: str, diff: str, body: str) -> BenchTask:
        return BenchTask(
            task_id=tid, difficulty=diff,
            ground_truth=GroundTruth(reproduces=False, notes="execution fails"),
            repo_files={"inference.py": body},
        )

    return [
        good("easy_ok_1", "easy", 0.91),
        broken("easy_broken_1", "easy", _BROKEN_EXIT),
        good("medium_ok_1", "medium", 0.84),
        broken("medium_broken_1", "medium", _BROKEN_IMPORT),
        good("hard_ok_1", "hard", 0.72),
        broken("hard_broken_1", "hard", _BROKEN_EXIT),
    ]


def load_tasks(path: str | None = None) -> list[BenchTask]:
    """Load an adapted CORE-Bench export (JSON list of tasks), or the bundled
    synthetic set when no path is given."""
    if path is None:
        return synthetic_tasks()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    tasks: list[BenchTask] = []
    for t in raw:
        gt = t.get("ground_truth", {})
        tasks.append(BenchTask(
            task_id=t["task_id"], difficulty=t.get("difficulty", "medium"),
            ground_truth=GroundTruth(reproduces=gt.get("reproduces", True),
                                     expected=gt.get("expected", {}), notes=gt.get("notes")),
            repo_files=t.get("repo_files", {}),
        ))
    return tasks


def stratified_sample(tasks: list[BenchTask], n: int, seed: int = 0) -> list[BenchTask]:
    """Deterministic round-robin sample across difficulty strata."""
    strata: dict[str, list[BenchTask]] = {}
    for t in sorted(tasks, key=lambda x: x.task_id):
        strata.setdefault(t.difficulty, []).append(t)
    order = sorted(strata)
    out: list[BenchTask] = []
    i = 0
    while len(out) < min(n, len(tasks)) and order:
        bucket = strata[order[i % len(order)]]
        pos = i // len(order)
        if pos < len(bucket):
            out.append(bucket[pos])
        i += 1
        if i > len(tasks) * 2:
            break
    return out
