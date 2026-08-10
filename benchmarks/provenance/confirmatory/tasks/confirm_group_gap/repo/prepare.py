"""Prepare the intermediate observations for confirm_group_gap."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

OPERATION = 'group_gap'


def _number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("input values must be finite")
    return value


def prepare(data_path: Path, output_path: Path, *, reverse: bool = False) -> None:
    with data_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("input must contain a non-empty rectangular CSV table")
    prepared: list[tuple[str, float]] = []
    if OPERATION == "geometric_growth":
        for row in rows:
            start = _number(row["start"])
            end = _number(row["end"])
            if start <= 0 or end <= 0:
                raise ValueError("growth endpoints must be positive")
            prepared.append((row["series"], end / start))
    elif OPERATION == "group_gap":
        for index, row in enumerate(rows):
            prepared.append((f"{row['group']}:{index}", _number(row["value"])))
    elif OPERATION == "normalized_gain":
        for row in rows:
            pre = _number(row["pre"])
            post = _number(row["post"])
            maximum = _number(row["maximum"])
            if maximum <= pre:
                raise ValueError("maximum must exceed pre")
            prepared.append((row["case_id"], (post - pre) / (maximum - pre)))
    elif OPERATION == "seeded_effect":
        for row in rows:
            prepared.append((row["seed"], _number(row["candidate"]) - _number(row["baseline"])))
    elif OPERATION == "trimmed_mean":
        for row in rows:
            prepared.append((row["observation_id"], _number(row["value"])))
    else:
        raise ValueError(f"unknown operation {OPERATION}")
    if reverse:
        prepared.reverse()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("key", "value"))
        for key, value in prepared:
            writer.writerow((key, format(value, ".17g")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("inputs/measurements.csv"))
    parser.add_argument("--output", type=Path, default=Path("work/prepared.csv"))
    args = parser.parse_args()
    prepare(args.data, args.output)


if __name__ == "__main__":
    main()
