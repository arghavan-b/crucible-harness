"""Reproduce the pinned CORE-Bench annotation map and public log archive.

Usage:
    python scripts/download_corebench_logs.py --mapping-only
    python scripts/download_corebench_logs.py \
        --expected-manifest data/corebench/public_log_checksums.json
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crucible.benchmarks.corebench_logs import cli


if __name__ == "__main__":
    raise SystemExit(cli())
