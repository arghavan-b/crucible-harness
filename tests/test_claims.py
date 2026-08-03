"""Claim intake: schema, policy gating, extraction, procedure compiler, adapter."""

from __future__ import annotations

import json
import os

import pytest

from crucible.claims import (
    AcceptancePolicy,
    ArtifactKind,
    AssayType,
    Availability,
    Claim,
    ClaimContext,
    ClaimIntake,
    ClaimType,
    DatasetRef,
    EvidenceRequirement,
    HeuristicExtractor,
    Margin,
    PolicySource,
    Relation,
    ReportedValues,
    SourceRef,
    SplitMethod,
    SplitSpec,
    Statement,
    compile_procedure,
    default_policy,
    ensure_policy,
    spec_from_claim,
    specs_from_claims,
)
from crucible.claims.adapter import dropped_requirements, slug
from crucible.intake.llm import FakeClient
from crucible.schemas import HypothesisType

REPORT = """\
# Results

We evaluate GNN-X on the TDC CYP2D6_Veith inhibition benchmark using a scaffold
split with a 70/10/20 ratio and seed = 42, featurized with Morgan r2
fingerprints. GNN-X outperforms the Random Forest baseline, achieving an AUROC
of 0.87 versus 0.81 across five seeds (± 0.01 standard deviation).
"""


def _claim(**overrides) -> Claim:
    base = dict(
        claim_id="claim-001",
        type=ClaimType.COMPARATIVE,
        statement=Statement(
            subject="GNN model X",
            relation=Relation.OUTPERFORMS,
            comparator="Random Forest baseline",
            margin=Margin(metric="AUROC", delta=0.06),
        ),
        context=ClaimContext(
            endpoint="CYP2D6_inhibition",
            assay_type=AssayType.BINARY_CLASSIFICATION,
            dataset=DatasetRef(name="TDC/CYP2D6_Veith", version="0.4.1"),
            split=SplitSpec(method=SplitMethod.SCAFFOLD, ratio=[0.7, 0.1, 0.2], seed=42),
        ),
        reported=ReportedValues(subject_value=0.87, comparator_value=0.81, metric="AUROC"),
        source=SourceRef(location="Table 2, p.5"),
        confidence=0.9,
    )
    base.update(overrides)
    return Claim(**base)


# --- schema + adjudicability gating ------------------------------------------


def test_claim_without_policy_is_not_adjudicable():
    ok, reason = _claim().is_adjudicable()
    assert not ok and reason == "no_acceptance_policy"


def test_ensure_policy_makes_claim_adjudicable_and_marks_it_generated():
    claim = ensure_policy(_claim())
    assert claim.acceptance_policy is not None
    assert claim.acceptance_policy.source is PolicySource.GENERATED
    assert claim.is_adjudicable() == (True, None)


def test_authored_policy_is_never_overwritten():
    authored = AcceptancePolicy(source=PolicySource.AUTHORED, min_seeds=9)
    claim = ensure_policy(_claim(acceptance_policy=authored))
    assert claim.acceptance_policy.min_seeds == 9
    assert claim.acceptance_policy.source is PolicySource.AUTHORED


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"statement": Statement(subject="X", comparator=None, margin=Margin(metric="AUROC"))},
         "comparative_claim_without_comparator"),
        ({"statement": Statement(subject="X", comparator="B"),
          "reported": ReportedValues(subject_value=0.9)},
         "no_metric_named"),
        ({"reported": ReportedValues(metric="AUROC")}, "no_reported_value"),
        ({"reported": ReportedValues(subject_value=0.87, metric="AUROC")}, "no_comparator_value"),
    ],
)
def test_adjudicability_reasons(overrides, expected):
    claim = ensure_policy(_claim(**overrides))
    ok, reason = claim.is_adjudicable()
    assert not ok and reason == expected


# --- acceptance policy: the constraints/requirements object -------------------


def test_scaffold_claim_requires_scaffold_integrity_and_split_parity():
    reqs = ensure_policy(_claim()).requirements()
    assert EvidenceRequirement.SCAFFOLD_SPLIT_INTEGRITY in reqs
    assert EvidenceRequirement.SPLIT_PARITY in reqs
    assert EvidenceRequirement.TEMPORAL_SPLIT_VALID not in reqs


def test_temporal_split_swaps_in_the_temporal_requirement():
    claim = _claim(
        context=ClaimContext(split=SplitSpec(method=SplitMethod.TEMPORAL), endpoint="clearance")
    )
    reqs = ensure_policy(claim).requirements()
    assert EvidenceRequirement.TEMPORAL_SPLIT_VALID in reqs
    assert EvidenceRequirement.SCAFFOLD_SPLIT_INTEGRITY not in reqs


def test_non_comparative_claim_drops_comparative_requirements():
    claim = _claim(
        type=ClaimType.REPRODUCTION,
        statement=Statement(subject="X", relation=Relation.ACHIEVES, comparator=None,
                            margin=Margin(metric="AUROC")),
        reported=ReportedValues(subject_value=0.87, metric="AUROC"),
    )
    reqs = ensure_policy(claim).requirements()
    assert EvidenceRequirement.MARGIN_SURVIVES_VARIANCE not in reqs
    assert EvidenceRequirement.SPLIT_PARITY not in reqs
    assert EvidenceRequirement.METRIC_COMPUTED_ON_TEST in reqs


def test_test_isolation_toggle_removes_leakage_requirements():
    policy = AcceptancePolicy(require_test_isolation=False)
    reqs = policy.requirements(_claim())
    assert EvidenceRequirement.PREPROCESSING_FIT_ON_TRAIN_ONLY not in reqs
    assert EvidenceRequirement.THRESHOLD_NOT_TUNED_ON_TEST not in reqs


def test_requirements_are_deduplicated():
    reqs = ensure_policy(_claim()).requirements()
    assert len(reqs) == len(set(reqs))


def test_bioactivity_endpoint_tightens_the_analog_threshold():
    admet = default_policy(endpoint="CYP2D6_inhibition")
    bioactivity = default_policy(endpoint="ChEMBL binding affinity IC50")
    assert bioactivity.require_dedup.analog_tanimoto < admet.require_dedup.analog_tanimoto
    assert "analog threshold tightened" in (bioactivity.notes or "")


def test_reproduction_claims_need_fewer_seeds_than_comparative():
    assert (
        default_policy(claim_type=ClaimType.REPRODUCTION).min_seeds
        < default_policy(claim_type=ClaimType.COMPARATIVE).min_seeds
    )


def test_unknown_split_is_recorded_as_a_note():
    policy = default_policy(split_method=SplitMethod.UNKNOWN)
    assert "split method not stated" in (policy.notes or "")


# --- extraction ---------------------------------------------------------------


def test_heuristic_extractor_reads_split_dataset_and_metric_from_a_report():
    claim_set = HeuristicExtractor().from_text(REPORT)
    assert claim_set.claims, "expected a comparative sentence to be found"
    claim = claim_set.claims[0]
    assert claim.context.split.method is SplitMethod.SCAFFOLD
    assert claim.context.split.seed == 42
    assert claim.context.split.ratio == [0.7, 0.1, 0.2]
    assert claim.reported.metric == "AUROC"
    assert claim.reported.subject_value == 0.87
    assert claim.reported.comparator_value == 0.81
    assert claim.reported.variance_reported is True
    assert any("TDC" in d.name for d in claim_set.datasets)


def test_heuristic_extraction_is_marked_low_confidence():
    claim = HeuristicExtractor().from_text(REPORT).claims[0]
    assert claim.confidence <= 0.3
    assert "review" in (claim.notes or "")


def test_heuristic_extractor_does_not_invent_a_split_method():
    text = "Model A outperforms model B, reaching an AUROC of 0.91 compared to 0.88."
    claim = HeuristicExtractor().from_text(text).claims[0]
    assert claim.context.split.method is SplitMethod.UNKNOWN


def test_heuristic_extractor_reports_when_nothing_was_found():
    claim_set = HeuristicExtractor().from_text("This paper introduces a new featurizer.")
    assert claim_set.claims == []
    assert "no comparative claim" in (claim_set.notes or "")


def test_llm_extraction_parses_into_typed_claims(tmp_path):
    payload = {
        "title": "GNN-X for CYP2D6",
        "claims": [
            {
                "claim_id": "claim-001",
                "type": "comparative_performance",
                "statement": {
                    "subject": "GNN-X",
                    "relation": "outperforms",
                    "comparator": "Random Forest",
                    "margin": {"metric": "AUROC", "delta": 0.06},
                },
                "context": {
                    "endpoint": "CYP2D6_inhibition",
                    "assay_type": "binary_classification",
                    "dataset": {"name": "TDC/CYP2D6_Veith"},
                    "split": {"method": "scaffold", "ratio": [0.7, 0.1, 0.2], "seed": 42},
                },
                "reported": {"subject_value": 0.87, "comparator_value": 0.81, "metric": "AUROC"},
                "source": {"location": "Table 2"},
                "confidence": 0.88,
            }
        ],
        "datasets": [{"name": "TDC/CYP2D6_Veith"}],
    }
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")

    result = ClaimIntake(llm=FakeClient([payload])).ingest(paper=str(report))
    claim = result.claims[0]
    assert claim.statement.subject == "GNN-X"
    assert claim.type is ClaimType.COMPARATIVE
    assert claim.context.split.method is SplitMethod.SCAFFOLD
    # The model never sets the policy; intake generates it.
    assert claim.acceptance_policy.source is PolicySource.GENERATED


# --- procedure compiler --------------------------------------------------------


def _make_repo(root, *, split_code=True, split_lists=True, seed=True, metric=True, baseline=True):
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    (root / "requirements.txt").write_text("rdkit\nscikit-learn\n", encoding="utf-8")
    if split_code:
        seed_line = "    np.random.seed(42)\n" if seed else "    pass\n"
        (root / "utils.py").write_text(
            "import numpy as np\n"
            "from deepchem.splits import ScaffoldSplitter\n"
            "def make_split(df):\n"
            f"{seed_line}"
            "    return ScaffoldSplitter().split(df)\n",
            encoding="utf-8",
        )
    if split_lists:
        for name in ("train", "valid", "test"):
            (root / "data" / f"{name}.csv").write_text("smiles,y\nCCO,1\n", encoding="utf-8")
    if metric:
        (root / "evaluate.py").write_text(
            "from sklearn.metrics import roc_auc_score\n"
            "def score(y, p):\n    return roc_auc_score(y, p)\n",
            encoding="utf-8",
        )
    if baseline:
        (root / "baseline.py").write_text(
            "from sklearn.ensemble import RandomForestClassifier\n"
            "clf = RandomForestClassifier(random_state=42)\n",
            encoding="utf-8",
        )


def test_compiler_locates_split_metric_and_baseline(tmp_path):
    _make_repo(tmp_path)
    report = compile_procedure(str(tmp_path))
    assert report.availability_of(ArtifactKind.SPLIT_CODE) is Availability.PRESENT
    assert report.availability_of(ArtifactKind.SPLIT_MOLECULE_LISTS) is Availability.PRESENT
    assert report.availability_of(ArtifactKind.METRIC_CODE) is Availability.PRESENT
    assert report.availability_of(ArtifactKind.BASELINE_CODE) is Availability.PRESENT
    assert report.blocking_reason() is None


def test_compiler_finds_a_splitter_defined_in_an_unhelpfully_named_file(tmp_path):
    _make_repo(tmp_path, split_lists=False)
    report = compile_procedure(str(tmp_path))
    finding = report.by_kind(ArtifactKind.SPLIT_CODE)
    assert finding.availability is Availability.PRESENT
    assert any(loc.file == "utils.py" for loc in finding.locations)


def test_split_lists_are_reconstructible_when_seed_is_pinned(tmp_path):
    _make_repo(tmp_path, split_lists=False, seed=True)
    report = compile_procedure(str(tmp_path))
    assert report.availability_of(ArtifactKind.SPLIT_MOLECULE_LISTS) is Availability.RECONSTRUCTIBLE


def test_split_lists_are_missing_when_no_seed_is_pinned(tmp_path):
    _make_repo(tmp_path, split_lists=False, seed=False)
    report = compile_procedure(str(tmp_path))
    finding = report.by_kind(ArtifactKind.SPLIT_MOLECULE_LISTS)
    assert finding.availability is Availability.MISSING
    assert "not regenerable" in (finding.detail or "")


def test_missing_split_code_and_lists_blocks_the_claim(tmp_path):
    _make_repo(tmp_path, split_code=False, split_lists=False)
    report = compile_procedure(str(tmp_path))
    assert report.blocking_reason() == "artifacts_unavailable"
    assert "BLOCKED" in report.summary()


def test_auditability_score_is_a_fraction_and_drops_when_artifacts_vanish(tmp_path):
    _make_repo(tmp_path)
    full = compile_procedure(str(tmp_path)).auditability_score
    sparse_root = tmp_path / "sparse"
    sparse_root.mkdir()
    _make_repo(sparse_root, split_code=False, split_lists=False, metric=False, baseline=False)
    sparse = compile_procedure(str(sparse_root)).auditability_score
    assert 0.0 <= sparse < full <= 1.0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("data/train.csv", True),
        ("data/2004-04_train.csv", True),       # date-prefixed — the common case
        ("data/test_fold1.csv", True),
        ("data/holdout.csv", True),
        ("data/latest.csv", False),            # 'test' inside a word, not a token
        ("data/pretrain.csv", False),
        ("data/trainer.py", False),
    ],
)
def test_split_list_naming_variants(tmp_path, name, expected):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("smiles,y\nCCO,1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    report = compile_procedure(str(tmp_path))
    found = report.availability_of(ArtifactKind.SPLIT_MOLECULE_LISTS) is Availability.PRESENT
    assert found is expected


def test_predictions_found_by_results_directory_not_just_filename(tmp_path):
    """`results/lp_res_0/CTGCN-C_auc_record.csv` mentions neither 'prediction'
    nor 'output' — filename-only matching reported no predictions shipped."""
    out = tmp_path / "results" / "lp_res_0"
    out.mkdir(parents=True)
    (out / "CTGCN-C_auc_record.csv").write_text("date,Avg\n2004-05,0.91\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    report = compile_procedure(str(tmp_path))
    assert report.availability_of(ArtifactKind.PREDICTIONS) is Availability.PRESENT


def test_split_files_under_results_are_not_reported_as_predictions(tmp_path):
    data = tmp_path / "results" / "lp_data_0"
    data.mkdir(parents=True)
    (data / "2004-04_train.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    report = compile_procedure(str(tmp_path))
    assert report.availability_of(ArtifactKind.SPLIT_MOLECULE_LISTS) is Availability.PRESENT
    # No genuine prediction file exists, so PREDICTIONS must not be satisfied
    # by the split file that happens to live under results/.
    assert report.availability_of(ArtifactKind.PREDICTIONS) is Availability.MISSING


def test_ratio_arithmetic_counts_as_split_code(tmp_path):
    """Repos that slice by `int(n * test_ratio)` never call a named splitter."""
    (tmp_path / "link_prediction.py").write_text(
        "class Splitter:\n"
        "    def __init__(self, train_ratio=0.5, val_ratio=0.2, test_ratio=0.3):\n"
        "        self.test_ratio = test_ratio\n"
        "    def run(self, edge_num):\n"
        "        test_num = int(edge_num * self.test_ratio)\n",
        encoding="utf-8",
    )
    report = compile_procedure(str(tmp_path))
    finding = report.by_kind(ArtifactKind.SPLIT_CODE)
    assert finding.availability is Availability.PRESENT
    assert any(loc.file == "link_prediction.py" for loc in finding.locations)


def test_compiler_separates_scientific_from_infrastructure_paths(tmp_path):
    _make_repo(tmp_path)
    report = compile_procedure(str(tmp_path))
    assert "requirements.txt" in report.infrastructure_path
    assert "evaluate.py" in report.scientific_path
    assert "requirements.txt" not in report.scientific_path


# --- intake orchestration ------------------------------------------------------


def test_repo_only_intake_reports_auditability_without_claims(tmp_path):
    _make_repo(tmp_path)
    result = ClaimIntake().ingest(repo=str(tmp_path))
    assert result.claims == []
    assert result.auditability > 0
    assert any("cannot be extracted from a repo alone" in w for w in result.warnings)


def test_paper_only_intake_is_blocked_on_artifacts(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")
    result = ClaimIntake().ingest(paper=str(report))
    assert result.claims
    assert result.blocked_reason == "artifacts_unavailable"
    assert result.adjudicable() == []


def test_paper_plus_repo_intake_produces_policied_claims(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")

    result = ClaimIntake().ingest(paper=str(report), repo=str(repo))
    assert result.blocked_reason is None
    assert all(c.acceptance_policy is not None for c in result.claims)
    assert result.artifacts.availability_of(ArtifactKind.SPLIT_CODE) is Availability.PRESENT


def test_explicit_policy_overrides_generation(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")
    authored = AcceptancePolicy(source=PolicySource.AUTHORED, min_seeds=11)
    result = ClaimIntake().ingest(paper=str(report), policy=authored)
    assert all(c.acceptance_policy.min_seeds == 11 for c in result.claims)


def test_intake_requires_at_least_one_input():
    with pytest.raises(ValueError):
        ClaimIntake().ingest()


def test_claim_set_round_trips_through_json(tmp_path):
    report = tmp_path / "report.md"
    report.write_text(REPORT, encoding="utf-8")
    result = ClaimIntake().ingest(paper=str(report))
    from crucible.claims import ClaimSet

    restored = ClaimSet.model_validate(json.loads(result.claim_set.model_dump_json()))
    assert len(restored.claims) == len(result.claims)
    assert restored.claims[0].acceptance_policy is not None


# --- adapter to ExperimentSpec -------------------------------------------------


def test_spec_from_claim_maps_comparison_and_seeds():
    claim = ensure_policy(_claim())
    spec = spec_from_claim(claim, repo_uri="local://repo")
    assert spec.hypothesis.type is HypothesisType.COMPARATIVE
    under_test = spec.claims_under_test[0]
    assert under_test.comparison == "gnn_model_x > random_forest_baseline"
    assert len(under_test.seeds) == claim.acceptance_policy.min_seeds
    assert under_test.reported_values["gnn_model_x"] == 0.87


def test_comparator_value_becomes_the_positive_control():
    spec = spec_from_claim(ensure_policy(_claim()), repo_uri="local://repo")
    control = spec.positive_controls[0]
    assert control.expected == 0.81
    assert "Reproduce reported baseline" in control.description


def test_claim_without_comparator_value_falls_back_to_mechanical_control():
    claim = ensure_policy(
        _claim(
            type=ClaimType.REPRODUCTION,
            statement=Statement(subject="X", relation=Relation.ACHIEVES,
                                margin=Margin(metric="AUROC")),
            reported=ReportedValues(subject_value=0.87, metric="AUROC"),
        )
    )
    control = spec_from_claim(claim, repo_uri="local://repo").positive_controls[0]
    assert control.metric == "smoke_exit_code"


def test_adapted_spec_always_has_a_positive_control():
    spec = spec_from_claim(ensure_policy(_claim()), repo_uri="local://repo")
    assert spec.has_positive_control()


def test_adapter_rejects_a_claim_with_no_policy():
    with pytest.raises(ValueError, match="not adjudicable"):
        spec_from_claim(_claim(), repo_uri="local://repo")


def test_specs_from_claims_skips_non_adjudicable_drafts():
    good = ensure_policy(_claim())
    draft = ensure_policy(_claim(claim_id="claim-002", reported=ReportedValues(metric="AUROC")))
    specs = specs_from_claims([good, draft], repo_uri="local://repo")
    assert len(specs) == 1


def test_dropped_requirements_names_what_the_executor_cannot_check():
    dropped = dropped_requirements(ensure_policy(_claim()))
    assert EvidenceRequirement.SPLIT_PARITY in dropped
    assert EvidenceRequirement.NO_ANALOG_LEAKAGE in dropped
    assert EvidenceRequirement.SEEDS_SUFFICIENT not in dropped


def test_slug_produces_identifiers_the_adjudicator_can_parse():
    assert slug("Random Forest baseline") == "random_forest_baseline"
    assert slug("GNN model X") == "gnn_model_x"
    # Leading digits would break the \w+ variable match, so they get prefixed.
    assert not slug("2-step model")[0].isdigit()
    assert slug("") == "value"


def test_adapted_spec_comparison_parses_in_the_adjudicator():
    import re

    from crucible.adjudicator.adjudicator import _COMPARISON_RE

    spec = spec_from_claim(ensure_policy(_claim()), repo_uri="local://repo")
    assert re.match(_COMPARISON_RE, spec.claims_under_test[0].comparison)
