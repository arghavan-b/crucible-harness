"""Run the required multi-stage pipeline for confirm_group_gap."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REPOSITORY = Path(__file__).resolve().parent
PREPARER = 'prepare_alternative.py'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("inputs/measurements.csv"))
    parser.add_argument("--calibration", type=Path, default=Path("inputs/calibration.csv"))
    args = parser.parse_args()
    subprocess.run([sys.executable, str(REPOSITORY / PREPARER), "--data", str(args.data)], check=True)
    subprocess.run(
        [sys.executable, str(REPOSITORY / "summarize.py"), "--calibration", str(args.calibration)],
        check=True,
    )


if __name__ == "__main__":
    main()
