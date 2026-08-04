# Weighted-mean analysis

This repository contains a small deterministic analysis over weighted observations and a
calibration dataset. The primary implementation is `pipeline.py`; `streaming_pipeline.py` is an
authorized one-pass implementation of the same calculation.

Run the primary analysis from this directory:

```bash
python pipeline.py
```

Both programs accept `--observations`, `--calibration`, and `--output`. By default they read the
two CSV files under `inputs/` and write canonical JSON to `outputs/result.json`. The output contains
the weighted mean and the maximum absolute calibration error.
