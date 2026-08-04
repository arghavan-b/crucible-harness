"""Run the authorized equivalent seeded-comparison pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


REPOSITORY = Path(__file__).resolve().parent


def run_pipeline(
    scores_path: Path,
    calibration_path: Path,
    output_path: Path,
    table_path: Path,
    work_path: Path,
) -> None:
    work_path.mkdir(parents=True, exist_ok=True)
    deltas_path = work_path / "deltas.csv"
    subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "prepare_alternative.py"),
            "--scores",
            str(scores_path),
            "--output",
            str(deltas_path),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(REPOSITORY / "summarize.py"),
            "--deltas",
            str(deltas_path),
            "--calibration",
            str(calibration_path),
            "--output",
            str(output_path),
            "--table",
            str(table_path),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", type=Path, default=Path("inputs/scores.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    parser.add_argument("--output", type=Path, default=Path("outputs/result.json"))
    parser.add_argument("--table", type=Path, default=Path("outputs/summary.csv"))
    parser.add_argument("--work", type=Path, default=Path("work"))
    args = parser.parse_args()
    run_pipeline(args.scores, args.calibration, args.output, args.table, args.work)


if __name__ == "__main__":
    main()
