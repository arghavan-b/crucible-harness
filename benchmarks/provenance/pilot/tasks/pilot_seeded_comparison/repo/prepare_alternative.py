"""Authorized alternative preparation for the seeded-comparison pilot."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


HEADER = ["seed", "baseline_accuracy", "candidate_accuracy"]
OUTPUT_HEADER = [*HEADER, "delta_pp"]


def _parse_decimal(text: str, *, row_number: int) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"row {row_number}: accuracy must be numeric") from exc
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("100"):
        raise ValueError(f"row {row_number}: accuracy is outside [0, 100]")
    return value


def _render(value: Decimal) -> str:
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def prepare_alternative(scores_path: Path, output_path: Path) -> None:
    prepared: dict[int, tuple[Decimal, Decimal]] = {}
    with scores_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != HEADER:
            raise ValueError(f"expected columns {HEADER}, got {header}")
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(HEADER):
                raise ValueError(f"row {row_number}: expected {len(HEADER)} columns")
            try:
                seed = int(row[0])
            except ValueError as exc:
                raise ValueError(f"row {row_number}: seed must be an integer") from exc
            if seed < 0:
                raise ValueError(f"row {row_number}: seed must be non-negative")
            if seed in prepared:
                raise ValueError(f"row {row_number}: duplicate seed {seed}")
            prepared[seed] = (
                _parse_decimal(row[1], row_number=row_number),
                _parse_decimal(row[2], row_number=row_number),
            )

    if not prepared:
        raise ValueError("scores input must contain at least one row")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUTPUT_HEADER)
        for seed in sorted(prepared):
            baseline, candidate = prepared[seed]
            writer.writerow(
                [seed, _render(baseline), _render(candidate), _render(candidate - baseline)]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, default=Path("inputs/scores.csv"))
    parser.add_argument("--output", type=Path, default=Path("work/deltas.csv"))
    args = parser.parse_args()
    prepare_alternative(args.scores, args.output)


if __name__ == "__main__":
    main()
