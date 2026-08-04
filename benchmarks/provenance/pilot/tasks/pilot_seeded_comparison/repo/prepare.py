"""Prepare paired per-seed improvements for the seeded-comparison pilot."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


FIELDNAMES = ("seed", "baseline_accuracy", "candidate_accuracy")
OUTPUT_FIELDNAMES = (*FIELDNAMES, "delta_pp")


def _decimal(text: str, *, field: str, row_number: int) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: {field} must be numeric") from exc
    if not value.is_finite():
        raise ValueError(f"row {row_number}: {field} must be finite")
    return value


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def prepare(scores_path: Path, output_path: Path) -> None:
    with scores_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != FIELDNAMES:
            raise ValueError(f"expected columns {FIELDNAMES}, got {reader.fieldnames}")

        rows: list[tuple[int, Decimal, Decimal]] = []
        seen_seeds: set[int] = set()
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(field) is None for field in FIELDNAMES):
                raise ValueError(f"row {row_number}: expected {len(FIELDNAMES)} columns")
            try:
                seed = int(row["seed"])
            except ValueError as exc:
                raise ValueError(f"row {row_number}: seed must be an integer") from exc
            if seed < 0:
                raise ValueError(f"row {row_number}: seed must be non-negative")
            if seed in seen_seeds:
                raise ValueError(f"row {row_number}: duplicate seed {seed}")
            seen_seeds.add(seed)

            baseline = _decimal(
                row["baseline_accuracy"], field="baseline_accuracy", row_number=row_number
            )
            candidate = _decimal(
                row["candidate_accuracy"], field="candidate_accuracy", row_number=row_number
            )
            if not (Decimal("0") <= baseline <= Decimal("100")):
                raise ValueError(f"row {row_number}: baseline_accuracy is outside [0, 100]")
            if not (Decimal("0") <= candidate <= Decimal("100")):
                raise ValueError(f"row {row_number}: candidate_accuracy is outside [0, 100]")
            rows.append((seed, baseline, candidate))

    if not rows:
        raise ValueError("scores input must contain at least one row")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUTPUT_FIELDNAMES)
        for seed, baseline, candidate in sorted(rows, key=lambda item: item[0]):
            writer.writerow(
                (
                    seed,
                    _format_decimal(baseline),
                    _format_decimal(candidate),
                    _format_decimal(candidate - baseline),
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, default=Path("inputs/scores.csv"))
    parser.add_argument("--output", type=Path, default=Path("work/deltas.csv"))
    args = parser.parse_args()
    prepare(args.scores, args.output)


if __name__ == "__main__":
    main()
