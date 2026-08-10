"""Allowlisted equivalent implementation for confirm_harmonic_mean."""

from __future__ import annotations

from pipeline import parse_args, run


def main() -> None:
    args = parse_args()
    run(
        args.data,
        args.calibration,
        args.query,
        args.output,
        args.table,
        args.figure,
        reverse=True,
    )


if __name__ == "__main__":
    main()
