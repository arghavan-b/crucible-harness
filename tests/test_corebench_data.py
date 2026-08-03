"""CORE-Bench data adapter tests (real capsule-7038571 metadata)."""

from __future__ import annotations

import math

from crucible.adjudicator.stats import prediction_interval_halfwidth, t_critical
from crucible.benchmarks.corebench_data import (
    CoreBenchTask,
    _reporting_halfwidth,
    find,
    load_core_bench,
    to_spec,
    tolerance_for,
    unscorable_questions,
)
from crucible.schemas import HypothesisType

# Real task metadata from siegelz/core-bench (core_train.json).
CAPSULE_7038571 = CoreBenchTask(
    capsule_id="capsule-7038571",
    capsule_title="K-Core based Temporal Graph Convolutional Network for Dynamic Graphs",
    field="Computer Science",
    language="Python",
    task_prompt="Run the main.py file three times. First, with config/uci.json, the "
                "preprocessing task, and the CTGCN-C method. Second, with config/uci.json, the "
                "embedding task, and the CTGCN-C method. Third, using python3 with config/uci.json "
                "and the link-pred task.",
    results=[
        {"Report the average AUC score of Had using the CTGCN-C method on the UCI dataset.": 0.9375660604380387},
        {"Report the average AUC score of Had using the CTGCN-C method on the UCI dataset.": 0.9372440957792072},
        {"Report the average AUC score of Had using the CTGCN-C method on the UCI dataset.": 0.931951440752941},
    ],
    capsule_doi="https://doi.org/10.24433/CO.9707317.v1",
)


def test_capsule_url() -> None:
    assert CAPSULE_7038571.capsule_url == \
        "https://corebench.cs.princeton.edu/capsules/capsule-7038571.tar.gz"


def test_to_spec_maps_answer_key() -> None:
    spec = to_spec(CAPSULE_7038571)
    assert spec.experiment_id == "exp_capsule_7038571"
    assert spec.hypothesis.type is HypothesisType.REPRODUCTION
    assert spec.has_positive_control()

    # One claim built from the (repeated) AUC question, averaged across the 3 runs.
    assert len(spec.claims_under_test) == 1
    claim = spec.claims_under_test[0]
    (_var, reported), = claim.reported_values.items()
    assert 0.934 < reported < 0.938              # mean of the three AUCs
    assert len(claim.seeds) == 3

    # Tolerance is the 95% prediction interval for a fourth run, so it must be
    # strictly wider than the spread the three known runs happen to show.
    values = [0.9375660604380387, 0.9372440957792072, 0.931951440752941]
    assert claim.tolerance.value > max(values) - min(values)
    assert claim.tolerance.value == prediction_interval_halfwidth(values)


def test_t_critical_matches_published_table() -> None:
    for df, want in [(1, 12.706), (2, 4.3027), (5, 2.5706), (30, 2.0423), (100, 1.9840)]:
        assert abs(t_critical(df, 0.05) - want) < 2e-3


def test_prediction_interval_needs_replicates() -> None:
    assert prediction_interval_halfwidth([0.5]) is None          # spread unknowable
    assert prediction_interval_halfwidth([0.5, 0.5, 0.5]) == 0.0  # deterministic
    # Wider than a confidence interval on the mean: covers a new run's own noise.
    vals = [0.1, 0.2, 0.3]
    ci = t_critical(2) * math.sqrt(0.01) / math.sqrt(3)
    assert prediction_interval_halfwidth(vals) > ci


def test_reporting_halfwidth_ignores_integral_values() -> None:
    assert _reporting_halfwidth(0.88) == 0.005      # 2dp -> [0.875, 0.885)
    assert _reporting_halfwidth(0.018) == 0.0005
    # Python prints every float with a decimal point; 0.0 and 1.0 are not
    # evidence of rounding to 1dp and must not claim a 0.05 half-width.
    assert _reporting_halfwidth(0.0) == 0.0
    assert _reporting_halfwidth(1.0) == 0.0
    assert _reporting_halfwidth(6) == 0.0


def test_tolerance_never_uses_a_magic_constant() -> None:
    """The old rule was max(0.01, max-min). 0.01 exceeded the whole value here."""
    fnmr = tolerance_for([0.01, 0.0, 0.018])        # capsule-3272782, mean 0.0093
    assert fnmr.basis == "prediction_interval"
    assert fnmr.value != 0.01
    assert fnmr.estimable

    # A deterministic count-valued answer gets a float-comparison floor, not 0.01.
    counts = tolerance_for([6, 6, 6])
    assert counts.basis == "isclose_floor"
    assert counts.value < 1e-3

    # A single rounded reference run is sized by its own precision, and says so.
    lone = tolerance_for([0.88])
    assert lone.basis == "reporting"
    assert lone.value == 0.005
    assert not lone.estimable


def test_unscorable_questions_are_not_silent() -> None:
    task = CoreBenchTask(
        capsule_id="capsule-0", capsule_title="t", field="CS", language="Python",
        task_prompt="run",
        results=[{"Name the best group.": "control", "Report the AUC.": 0.9},
                 {"Name the best group.": "control", "Report the AUC.": 0.9}],
    )
    dropped = unscorable_questions(task)
    assert list(dropped) == ["Name the best group."]
    # ...and the numeric one still becomes a claim.
    assert len(to_spec(task).claims_under_test) == 1


def test_load_and_find(tmp_path) -> None:
    import json
    p = tmp_path / "core.json"
    p.write_text(json.dumps([{
        "capsule_id": "capsule-7038571", "capsule_title": "t", "field": "CS",
        "language": "Python", "task_prompt": "run", "results": [{"q": 0.9}],
    }]))
    tasks = load_core_bench(str(p))
    assert find(tasks, "7038571").capsule_title == "t"
    assert to_spec(find(tasks, "capsule-7038571")).claims_under_test
