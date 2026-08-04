"""Summarize paired improvements and the calibration positive control."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


DELTA_FIELDS = ("seed", "baseline_accuracy", "candidate_accuracy", "delta_pp")
CALIBRATION_FIELDS = ("example_id", "label", "prediction")
TASK_ID = "pilot_seeded_comparison"


def _finite_decimal(text: str, *, field: str, row_number: int) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: {field} must be numeric") from exc
    if not value.is_finite():
        raise ValueError(f"row {row_number}: {field} must be finite")
    return value


def _read_deltas(path: Path) -> list[tuple[int, Decimal]]:
    values: list[tuple[int, Decimal]] = []
    seen_seeds: set[int] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != DELTA_FIELDS:
            raise ValueError(f"expected columns {DELTA_FIELDS}, got {reader.fieldnames}")
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(field) is None for field in DELTA_FIELDS):
                raise ValueError(f"row {row_number}: expected {len(DELTA_FIELDS)} columns")
            try:
                seed = int(row["seed"])
            except ValueError as exc:
                raise ValueError(f"row {row_number}: seed must be an integer") from exc
            if seed < 0:
                raise ValueError(f"row {row_number}: seed must be non-negative")
            if seed in seen_seeds:
                raise ValueError(f"row {row_number}: duplicate seed {seed}")
            seen_seeds.add(seed)
            baseline = _finite_decimal(
                row["baseline_accuracy"],
                field="baseline_accuracy",
                row_number=row_number,
            )
            candidate = _finite_decimal(
                row["candidate_accuracy"],
                field="candidate_accuracy",
                row_number=row_number,
            )
            delta = _finite_decimal(row["delta_pp"], field="delta_pp", row_number=row_number)
            if not (Decimal("0") <= baseline <= Decimal("100")):
                raise ValueError(f"row {row_number}: baseline_accuracy is outside [0, 100]")
            if not (Decimal("0") <= candidate <= Decimal("100")):
                raise ValueError(f"row {row_number}: candidate_accuracy is outside [0, 100]")
            if delta != candidate - baseline:
                raise ValueError(f"row {row_number}: delta_pp does not match the scores")
            values.append((seed, delta))
    if not values:
        raise ValueError("deltas input must contain at least one row")
    return sorted(values)


def _read_calibration(path: Path) -> list[tuple[str, str, str]]:
    values: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CALIBRATION_FIELDS:
            raise ValueError(f"expected columns {CALIBRATION_FIELDS}, got {reader.fieldnames}")
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(field) is None for field in CALIBRATION_FIELDS):
                raise ValueError(f"row {row_number}: expected {len(CALIBRATION_FIELDS)} columns")
            example_id = row["example_id"].strip()
            if not example_id:
                raise ValueError(f"row {row_number}: example_id must not be empty")
            if example_id in seen_ids:
                raise ValueError(f"row {row_number}: duplicate example_id {example_id!r}")
            seen_ids.add(example_id)
            label = row["label"].strip()
            prediction = row["prediction"].strip()
            if not label or not prediction:
                raise ValueError(f"row {row_number}: label and prediction must not be empty")
            values.append((example_id, label, prediction))
    if not values:
        raise ValueError("calibration input must contain at least one row")
    return sorted(values)


def summarize(
    deltas_path: Path,
    calibration_path: Path,
    output_path: Path,
    table_path: Path,
) -> None:
    deltas = _read_deltas(deltas_path)
    calibration = _read_calibration(calibration_path)

    mean_delta = sum((value for _, value in deltas), Decimal("0")) / Decimal(len(deltas))
    correct = sum(label == prediction for _, label, prediction in calibration)
    calibration_accuracy = Decimal(correct) / Decimal(len(calibration))
    control_passed = calibration_accuracy == Decimal("1")

    result = {
        "calibration_accuracy": float(calibration_accuracy),
        "mean_delta_pp": float(mean_delta),
        "n_calibration": len(calibration),
        "n_seeds": len(deltas),
        "positive_control_passed": control_passed,
        "schema_version": 1,
        "task_id": TASK_ID,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    table_path.parent.mkdir(parents=True, exist_ok=True)
    with table_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("metric", "value"))
        writer.writerow(("calibration_accuracy", format(calibration_accuracy, "f")))
        writer.writerow(("mean_delta_pp", format(mean_delta, "f")))
        writer.writerow(("n_calibration", len(calibration)))
        writer.writerow(("n_seeds", len(deltas)))
        writer.writerow(("positive_control_passed", str(control_passed).lower()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deltas", type=Path, default=Path("work/deltas.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    parser.add_argument("--output", type=Path, default=Path("outputs/result.json"))
    parser.add_argument("--table", type=Path, default=Path("outputs/summary.csv"))
    args = parser.parse_args()
    summarize(args.deltas, args.calibration, args.output, args.table)


if __name__ == "__main__":
    main()
