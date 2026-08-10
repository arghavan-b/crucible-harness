"""Run the required direct scientific analysis for confirm_regression_slope."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path

TASK_ID = 'confirm_regression_slope'
METRIC = 'slope'
OPERATION = 'regression_slope'


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"{path} must contain a non-empty rectangular CSV table")
    return rows


def _number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("input values must be finite")
    return value


def compute(data_path: Path, query_path: Path | None, *, reverse: bool = False) -> tuple[float, int]:
    rows = _rows(data_path)
    if reverse:
        rows.reverse()
    if OPERATION == "auc":
        positives = [_number(row["score"]) for row in rows if row["label"] == "1"]
        negatives = [_number(row["score"]) for row in rows if row["label"] == "0"]
        if not positives or not negatives:
            raise ValueError("AUC requires both classes")
        wins = sum(1.0 if positive > negative else 0.5 if positive == negative else 0.0
                   for positive in positives for negative in negatives)
        metric = wins / (len(positives) * len(negatives))
    elif OPERATION == "harmonic_mean":
        values = [_number(row["rate"]) for row in rows]
        if any(value <= 0 for value in values):
            raise ValueError("rates must be positive")
        metric = len(values) / sum(1.0 / value for value in values)
    elif OPERATION == "regression_slope":
        points = [(_number(row["x"]), _number(row["y"])) for row in rows]
        mean_x = sum(x for x, _ in points) / len(points)
        mean_y = sum(y for _, y in points) / len(points)
        denominator = sum((x - mean_x) ** 2 for x, _ in points)
        if denominator == 0:
            raise ValueError("slope requires variation in x")
        metric = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    elif OPERATION == "weighted_median":
        values = sorted((_number(row["value"]), _number(row["weight"])) for row in rows)
        if any(weight <= 0 for _, weight in values):
            raise ValueError("weights must be positive")
        half = sum(weight for _, weight in values) / 2.0
        cumulative = 0.0
        metric = values[-1][0]
        for value, weight in values:
            cumulative += weight
            if cumulative >= half:
                metric = value
                break
    elif OPERATION == "sql_threshold_rate":
        if query_path is None:
            raise ValueError("the SQLite task requires a query")
        query = query_path.read_text(encoding="utf-8")
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE TABLE measurements(sample_id TEXT PRIMARY KEY, value REAL)")
            connection.executemany(
                "INSERT INTO measurements(sample_id, value) VALUES (?, ?)",
                [(row["sample_id"], _number(row["value"])) for row in rows],
            )
            observed = connection.execute(query).fetchone()
        finally:
            connection.close()
        if observed is None or observed[0] is None:
            raise ValueError("query returned no aggregate")
        metric = _number(str(observed[0]))
    else:
        raise ValueError(f"unknown operation {OPERATION}")
    return metric, len(rows)


def calibration_error(path: Path) -> float:
    rows = _rows(path)
    return max(abs(_number(row["observed"]) - _number(row["expected"])) for row in rows)


def write_outputs(output: Path, table: Path | None, figure: Path | None, result: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    if table is not None:
        table.parent.mkdir(parents=True, exist_ok=True)
        rows = ["metric,value"]
        for key in sorted(key for key in result if key != "task_id"):
            value = result[key]
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            rows.append(f"{key},{rendered}")
        table.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if figure is not None:
        figure.parent.mkdir(parents=True, exist_ok=True)
        figure.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="100" viewBox="0 0 320 100">\n'
            '  <rect width="320" height="100" fill="white"/>\n'
            f'  <text x="16" y="38">{METRIC}</text>\n'
            f'  <text x="16" y="72">{result[METRIC]}</text>\n'
            '</svg>\n',
            encoding="utf-8",
        )


def run(data: Path, calibration: Path, query: Path | None, output: Path, table: Path | None, figure: Path | None, *, reverse: bool = False) -> None:
    metric, count = compute(data, query, reverse=reverse)
    control_error = calibration_error(calibration)
    result = {
        "calibration_max_abs_error": control_error,
        METRIC: metric,
        "n_records": count,
        "positive_control_passed": control_error == 0.0,
        "schema_version": 1,
        "task_id": TASK_ID,
    }
    write_outputs(output, table, figure, result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("inputs/measurements.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    parser.add_argument("--query", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("outputs/result.json"))
    parser.add_argument("--table", type=Path, default=None)
    parser.add_argument("--figure", type=Path, default=Path("outputs/fit.svg"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(args.data, args.calibration, args.query, args.output, args.table, args.figure)


if __name__ == "__main__":
    main()
