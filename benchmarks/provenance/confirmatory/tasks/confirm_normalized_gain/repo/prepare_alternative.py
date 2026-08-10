"""Allowlisted alternative preparation order for confirm_normalized_gain."""

from __future__ import annotations

import argparse
from pathlib import Path

from prepare import prepare


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("inputs/measurements.csv"))
    parser.add_argument("--output", type=Path, default=Path("work/prepared.csv"))
    args = parser.parse_args()
    prepare(args.data, args.output, reverse=True)


if __name__ == "__main__":
    main()
