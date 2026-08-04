# Seeded comparison pilot

This repository contains a small, deterministic two-stage analysis. For every
seed in `inputs/scores.csv`, the preparation stage computes the candidate's
accuracy minus the baseline's accuracy. The summary stage reports the mean
paired improvement and evaluates the supplied calibration predictions.

The scientific claim is that the candidate improves mean accuracy by at least
10 percentage points. The accepted primary artifact is
`outputs/result.json`; `outputs/summary.csv` is a deterministic tabular view of
the same measurements.

Run the required pipeline from this directory:

```console
python run_pipeline.py
```

An explicitly authorized equivalent implementation is also available:

```console
python run_alternative.py
```

Both entry points accept `--scores`, `--calibration`, `--output`, `--table`,
and `--work` arguments. They use only the Python standard library.
