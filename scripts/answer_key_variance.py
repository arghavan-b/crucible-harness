"""Answer-key variance in CORE-Bench.

CORE-Bench ships an answer key of N independent reference runs per task
(`core_train.json`, `results` field). This script measures how much those
reference runs disagree with each other.

Why it matters: a verifier's tolerance must at minimum cover the spread of the
benchmark's own ground truth. Any tolerance tighter than that marks correct
reproductions wrong.

This script is the evidence behind `corebench_data.tolerance_for`, which sizes
tolerance from a 95% prediction interval rather than the `max(0.01, max-min)`
rule it replaced.

Usage:
    python3 scripts/answer_key_variance.py [path/to/core_train.json]
"""

from __future__ import annotations

import collections
import json
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crucible.benchmarks.corebench_data import tolerance_for


def numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def by_question(task: dict) -> dict[str, list[float]]:
    out: dict[str, list[float]] = collections.defaultdict(list)
    for run in task.get("results", []):
        if not isinstance(run, dict):
            continue
        for question, value in run.items():
            num = numeric(value)
            if num is not None:
                out[question].append(num)
    return dict(out)


def main(path: str = "core_train.json") -> None:
    with open(path, encoding="utf-8") as fh:
        tasks = json.load(fh)

    rows = []
    n_questions = 0
    n_multirun = 0
    for task in tasks:
        worst_abs = 0.0
        worst_rel = 0.0
        n_vary = 0
        n_q = 0
        for _question, vals in by_question(task).items():
            n_questions += 1
            if len(vals) < 2:
                continue
            n_multirun += 1
            n_q += 1
            spread = max(vals) - min(vals)
            mean = st.fmean(vals)
            if spread > 0:
                n_vary += 1
                worst_abs = max(worst_abs, spread)
                worst_rel = max(worst_rel, spread / abs(mean) if mean else 0.0)
        rows.append({
            "capsule_id": task.get("capsule_id"),
            "language": task.get("language"),
            "field": task.get("field"),
            "n_multirun_questions": n_q,
            "n_varying": n_vary,
            "worst_abs_spread": worst_abs,
            "worst_rel_spread": worst_rel,
        })

    print(f"tasks: {len(tasks)}   numeric questions: {n_questions}   "
          f"with >1 reference run: {n_multirun}\n")

    for lang in sorted({r["language"] for r in rows if r["language"]}):
        grp = [r for r in rows if r["language"] == lang and r["n_multirun_questions"] > 0]
        flaky = [r for r in grp if r["n_varying"] > 0]
        pct = len(flaky) / len(grp) if grp else 0.0
        print(f"{lang:8s} {len(grp):3d} tasks with multi-run answers, "
              f"{len(flaky):2d} nondeterministic ({pct:.0%})")

    scored = [r for r in rows if r["n_multirun_questions"] > 0]
    flaky = [r for r in scored if r["n_varying"] > 0]
    print(f"{'ALL':8s} {len(scored):3d} tasks with multi-run answers, "
          f"{len(flaky):2d} nondeterministic ({len(flaky) / max(1, len(scored)):.0%})\n")

    print("Nondeterministic tasks (worst question per task):")
    print(f"  {'capsule':20s} {'lang':6s} {'field':18s} {'abs':>10s} {'rel':>9s}")
    for r in sorted(flaky, key=lambda r: -r["worst_rel_spread"]):
        print(f"  {r['capsule_id']:20s} {r['language']:6s} {r['field']:18s} "
              f"{r['worst_abs_spread']:10.6g} {r['worst_rel_spread']:8.2%}")

    print("\nTolerance, old rule vs current:")
    print(f"  {'capsule':20s} {'reported':>12s} {'max(0.01,sp)':>13s} {'tolerance_for':>14s}  basis")
    for task in tasks:
        for _question, vals in by_question(task).items():
            if len(vals) < 2 or max(vals) - min(vals) == 0:
                continue
            raw = [v for run in task.get("results", []) for q2, v in run.items()
                   if q2 == _question and numeric(v) is not None]
            old = max(0.01, max(vals) - min(vals))
            tol = tolerance_for(raw)
            print(f"  {task['capsule_id']:20s} {st.fmean(vals):12.6g} "
                  f"{old:13.5g} {tol.value:14.5g}  {tol.basis}")

    print("\nNote: CORE-Bench's own grader uses a 95% prediction interval widened by\n"
          "np.isclose defaults, so it already accommodates this — it is not a bug in\n"
          "their benchmark. The claim here is narrower: any *reimplementation* of the\n"
          "grader that assumes determinism is wrong, and a fixed absolute floor is the\n"
          "wrong shape for small-magnitude metrics (see the FNMR case, where the\n"
          "reference spread exceeds the reported value itself).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "core_train.json")
