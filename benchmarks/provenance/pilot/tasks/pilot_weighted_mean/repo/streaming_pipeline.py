"""Authorized one-pass implementation of the weighted-mean pilot task."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, default=Path("inputs/observations.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    parser.add_argument("--output", type=Path, default=Path("outputs/result.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    weighted_total = 0.0
    total_weight = 0.0
    with args.observations.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames != ["value", "weight"]:
            raise ValueError(f"{args.observations} must have columns: value,weight")
        for row in rows:
            value = float(row["value"])
            weight = float(row["weight"])
            if not math.isfinite(value) or not math.isfinite(weight):
                raise ValueError(f"{args.observations} contains a non-finite number")
            if weight <= 0:
                raise ValueError(f"{args.observations} contains a non-positive weight")
            weighted_total += value * weight
            total_weight += weight
    if total_weight == 0:
        raise ValueError(f"{args.observations} contains no observations")

    calibration_max_abs_error = 0.0
    calibration_count = 0
    with args.calibration.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames != ["expected", "observed"]:
            raise ValueError(f"{args.calibration} must have columns: expected,observed")
        for row in rows:
            expected = float(row["expected"])
            observed = float(row["observed"])
            if not math.isfinite(expected) or not math.isfinite(observed):
                raise ValueError(f"{args.calibration} contains a non-finite number")
            calibration_max_abs_error = max(
                calibration_max_abs_error,
                abs(observed - expected),
            )
            calibration_count += 1
    if calibration_count == 0:
        raise ValueError(f"{args.calibration} contains no calibration rows")

    result = {
        "calibration_max_abs_error": calibration_max_abs_error,
        "task_id": "pilot_weighted_mean",
        "weighted_mean": weighted_total / total_weight,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(result, allow_nan=False, sort_keys=True, separators=(",", ":"))
    args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
