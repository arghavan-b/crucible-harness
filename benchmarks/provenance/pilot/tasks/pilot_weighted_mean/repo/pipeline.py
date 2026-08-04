"""Compute the pilot task's weighted mean and calibration control."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def _weighted_mean(path: Path) -> float:
    weighted_total = 0.0
    total_weight = 0.0
    with path.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames != ["value", "weight"]:
            raise ValueError(f"{path} must have columns: value,weight")
        for row in rows:
            value = float(row["value"])
            weight = float(row["weight"])
            if not math.isfinite(value) or not math.isfinite(weight):
                raise ValueError(f"{path} contains a non-finite number")
            if weight <= 0:
                raise ValueError(f"{path} contains a non-positive weight")
            weighted_total += value * weight
            total_weight += weight
    if total_weight == 0:
        raise ValueError(f"{path} contains no observations")
    return weighted_total / total_weight


def _calibration_max_abs_error(path: Path) -> float:
    maximum = 0.0
    count = 0
    with path.open(encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames != ["expected", "observed"]:
            raise ValueError(f"{path} must have columns: expected,observed")
        for row in rows:
            expected = float(row["expected"])
            observed = float(row["observed"])
            if not math.isfinite(expected) or not math.isfinite(observed):
                raise ValueError(f"{path} contains a non-finite number")
            maximum = max(maximum, abs(observed - expected))
            count += 1
    if count == 0:
        raise ValueError(f"{path} contains no calibration rows")
    return maximum


def _write_result(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(result, allow_nan=False, sort_keys=True, separators=(",", ":"))
    path.write_text(encoded + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--observations",
        type=Path,
        default=Path("inputs/observations.csv"),
        help="CSV containing value and weight columns",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=Path("inputs/calibration.csv"),
        help="CSV containing expected and observed control values",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/result.json"),
        help="Path for the canonical JSON result",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = {
        "calibration_max_abs_error": _calibration_max_abs_error(args.calibration),
        "task_id": "pilot_weighted_mean",
        "weighted_mean": _weighted_mean(args.observations),
    }
    _write_result(args.output, result)


if __name__ == "__main__":
    main()
