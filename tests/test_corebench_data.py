"""CORE-Bench data adapter tests (real capsule-7038571 metadata)."""

from __future__ import annotations

from crucible.benchmarks.corebench_data import CoreBenchTask, find, load_core_bench, to_spec
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
    (var, reported), = claim.reported_values.items()
    assert 0.934 < reported < 0.938              # mean of the three AUCs
    assert claim.tolerance.value >= 0.005        # covers the spread
    assert len(claim.seeds) == 3


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
