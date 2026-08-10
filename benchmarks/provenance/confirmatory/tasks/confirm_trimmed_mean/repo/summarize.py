"""Summarize prepared observations for confirm_trimmed_mean."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

TASK_ID = 'confirm_trimmed_mean'
METRIC = 'trimmed_mean'
OPERATION = 'trimmed_mean'


def _number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("values must be finite")
    return value


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("input must contain a non-empty rectangular CSV table")
    return rows


def summarize(prepared_path: Path, calibration_path: Path, output_path: Path, table_path: Path) -> None:
    rows = _read(prepared_path)
    values = [(row["key"], _number(row["value"])) for row in rows]
    if OPERATION == "geometric_growth":
        metric = math.prod(value for _, value in values) ** (1.0 / len(values))
    elif OPERATION == "group_gap":
        groups: dict[str, list[float]] = {}
        for key, value in values:
            groups.setdefault(key.split(":", 1)[0], []).append(value)
        if set(groups) != {"A", "B"}:
            raise ValueError("group-gap input requires groups A and B")
        means = {group: sum(items) / len(items) for group, items in groups.items()}
        metric = abs(means["B"] - means["A"])
    elif OPERATION in {"normalized_gain", "seeded_effect"}:
        metric = sum(value for _, value in values) / len(values)
    elif OPERATION == "trimmed_mean":
        ordered = sorted(value for _, value in values)
        if len(ordered) < 3:
            raise ValueError("trimmed mean requires at least three values")
        metric = sum(ordered[1:-1]) / len(ordered[1:-1])
    else:
        raise ValueError(f"unknown operation {OPERATION}")

    calibration = _read(calibration_path)
    control_error = max(abs(_number(row["observed"]) - _number(row["expected"])) for row in calibration)
    result = {
        "calibration_max_abs_error": control_error,
        METRIC: metric,
        "n_records": len(values),
        "positive_control_passed": control_error == 0.0,
        "schema_version": 1,
        "task_id": TASK_ID,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    table_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["metric,value"]
    for key in sorted(key for key in result if key != "task_id"):
        value = result[key]
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        lines.append(f"{key},{rendered}")
    table_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, default=Path("work/prepared.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    parser.add_argument("--output", type=Path, default=Path("outputs/result.json"))
    parser.add_argument("--table", type=Path, default=Path("outputs/summary.csv"))
    args = parser.parse_args()
    summarize(args.prepared, args.calibration, args.output, args.table)


if __name__ == "__main__":
    main()
