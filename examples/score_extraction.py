"""Scored extraction test — paper + repo, checked against a real answer key.

Most extraction demos show output and let you eyeball it. This one scores,
because the CTGCN case is one of the few where every layer has ground truth:

  - the paper (arXiv 2003.09902) states CTGCN-C = 0.9434 AUC on UCI (Table 4)
  - the repo is the exact Code Ocean capsule the paper was published with
  - CORE-Bench recorded what the capsule *actually produces* over three runs

So it tests three different things at once, and they disagree in an interesting
way: the reported number and the reproduced number are not the same, which is
exactly the situation the verdict layer exists to adjudicate.

Run with:  python -m examples.score_extraction
"""

from __future__ import annotations

import json
import os
from statistics import fmean

from crucible.claims import ClaimIntake, HeuristicExtractor, spec_from_claim
from crucible.intake.llm import FakeClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
CAPSULE = os.path.join(ROOT, "capsule-7038571", "capsule-7038571")
ANSWER_KEY = os.path.join(ROOT, "core_train.json")


def _paper() -> tuple[str, str]:
    """Prefer a real PDF so the pdfplumber path is exercised; fall back to the
    text excerpt. Returns (path, description-of-what-this-actually-tests)."""
    real = os.path.join(FIXTURES, "ctgcn_paper.pdf")
    if os.path.exists(real):
        return real, "real arXiv PDF (two-column; full parsing path)"
    try:
        import sys

        sys.path.insert(0, FIXTURES)
        from make_pdf import build

        generated = build(os.path.join(FIXTURES, "ctgcn_paper_generated.pdf"))
        return generated, "generated PDF (single-column; parsing path exercised)"
    except ImportError:
        return (
            os.path.join(FIXTURES, "ctgcn_paper.txt"),
            "text excerpt (PDF parsing NOT exercised — install reportlab)",
        )


PAPER, PAPER_KIND = _paper()

# --- ground truth -------------------------------------------------------------

PAPER_TRUTH = {
    "subject": "CTGCN-C",
    "comparator_best_baseline": "CGCN-S",
    "metric": "AUC",
    "dataset": "UCI",
    "subject_value": 0.9434,      # Table 4, UCI column
    "comparator_value": 0.9375,   # best non-CTGCN method on UCI
}

RUN_TRUTH = {
    "entry_point": "main.py",
    "config": "config/uci.json",
    "tasks": ["preprocessing", "embedding", "link_pred"],
    "method": "CTGCN-C",
    "split": {"train_ratio": "0.5", "val_ratio": "0.3", "test_ratio": "0.2"},
    "seed_pinned": False,
}

# What a competent extractor should return for this paper. Recorded rather than
# live so the test is deterministic and costs nothing; swap in a real client to
# measure the model itself.
RECORDED = {
    "title": "K-Core based Temporal Graph Convolutional Network for Dynamic Graphs",
    "claims": [
        {
            "claim_id": "claim-001",
            "type": "comparative_performance",
            "statement": {
                "subject": "CTGCN-C",
                "relation": "outperforms",
                "comparator": "CGCN-S",
                "margin": {"metric": "AUC", "delta": 0.0059},
                "text": "the proposed CTGCN-C method significantly outperforms other "
                        "compared methods across all dynamic graphs",
            },
            "context": {
                "endpoint": "link_prediction",
                "assay_type": "binary_classification",
                "dataset": {"name": "UCI"},
                "split": {"method": "temporal", "ratio": [0.5, 0.3, 0.2],
                          "seed": None, "date_field": "month"},
                "representation": {"featurizer": "one-hot node features, d=128"},
            },
            "reported": {
                "subject_value": 0.9434,
                "comparator_value": 0.9375,
                "metric": "AUC",
                "variance_reported": False,
            },
            "source": {"location": "Table 4, p.7",
                       "quote": "CTGCN-C 0.9434 ... CGCN-S 0.9375"},
            "confidence": 0.9,
        }
    ],
    "datasets": [{"name": "UCI"}],
}


def _check(label: str, got, want, tol: float | None = None) -> bool:
    if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:34s} got={got!r} want={want!r}")
    return ok


def main() -> None:
    results: list[bool] = []

    # --- 1. unaided offline extraction ---------------------------------------
    print("=" * 78)
    print("1. HEURISTIC EXTRACTION (offline, no LLM) — what it gets unaided")
    print("=" * 78)
    print(f"  paper: {os.path.basename(PAPER)}  [{PAPER_KIND}]")
    if PAPER.endswith(".pdf"):
        from crucible.intake.paper import parse_pdf

        parsed = parse_pdf(PAPER, include_figures=False)
        print(f"  parsed: {len(parsed.page_text)} page(s), "
              f"{len(parsed.tables)} table(s) recovered as grids")
        offline = HeuristicExtractor().extract(PAPER)
    else:
        text = open(PAPER, encoding="utf-8").read()
        offline = HeuristicExtractor().from_text(text, paper_path=PAPER)
    print(f"  claims found: {len(offline.claims)}")
    for c in offline.claims[:3]:
        print(f"    {c.claim_id}: metric={c.reported.metric} "
              f"subject_value={c.reported.subject_value} "
              f"comparator_value={c.reported.comparator_value} conf={c.confidence}")
        print(f"      split={c.context.split.method.value} "
              f"dataset={c.context.dataset.name if c.context.dataset else None}")
        ok, reason = c.is_adjudicable()
        print(f"      adjudicable={ok} ({reason})")
    print("  -> the offline path finds the sentence and the metric but will not name\n"
          "     the method or baseline; it refuses to guess, and says so.")

    # --- 2. LLM extraction + repo compilation --------------------------------
    print()
    print("=" * 78)
    print("2. LLM EXTRACTION + PROCEDURE COMPILER (paper + real capsule)")
    print("=" * 78)
    result = ClaimIntake(llm=FakeClient([RECORDED])).ingest(paper=PAPER, repo=CAPSULE)
    claim = result.claims[0]

    print("\n claim fields vs the paper:")
    results += [
        _check("subject", claim.statement.subject, PAPER_TRUTH["subject"]),
        _check("comparator", claim.statement.comparator,
               PAPER_TRUTH["comparator_best_baseline"]),
        _check("metric", claim.metric, PAPER_TRUTH["metric"]),
        _check("dataset", claim.context.dataset.name if claim.context.dataset else None,
               PAPER_TRUTH["dataset"]),
        _check("reported subject value", claim.reported.subject_value,
               PAPER_TRUTH["subject_value"], tol=1e-6),
        _check("reported comparator value", claim.reported.comparator_value,
               PAPER_TRUTH["comparator_value"], tol=1e-6),
    ]

    # --- 3. run config -------------------------------------------------------
    print("\n run config vs the capsule (fully unaided — no LLM involved):")
    run = result.artifacts.run_config
    cmds = run.reproduce_commands
    results += [
        _check("entry point", cmds[0].entry_point if cmds else None,
               RUN_TRUTH["entry_point"]),
        _check("config referenced", run.configs_referenced(), [RUN_TRUTH["config"]]),
        _check("task sequence", [c.args.get("task") for c in cmds], RUN_TRUTH["tasks"]),
        _check("method flag", cmds[0].args.get("method") if cmds else None,
               RUN_TRUTH["method"]),
        _check("seed pinned", bool(run.declared_seeds()), RUN_TRUTH["seed_pinned"]),
    ]
    declared = {k.split(":")[-1].split(".")[-1]: v for k, v in run.declared_split().items()}
    for key, want in RUN_TRUTH["split"].items():
        results.append(_check(f"declared {key}", declared.get(key), want))

    print(f"\n  auditability score: {result.auditability:.0%}")
    print(f"  blocking reason:    {result.blocked_reason}")

    # --- 4. paper's split claim vs what the code ran -------------------------
    print("\n cross-check — does the paper's split match the config?")
    claimed_ratio = claim.context.split.ratio
    actual = [float(declared.get(k, 0)) for k in ("train_ratio", "val_ratio", "test_ratio")]
    match = claimed_ratio == actual
    print(f"  claimed in paper: {claimed_ratio}   config on disk: {actual}   "
          f"{'consistent' if match else 'MISMATCH -> claimed_split_is_actual would FAIL'}")

    # --- 5. reported vs actually reproduced ----------------------------------
    print("\n" + "=" * 78)
    print("3. REPORTED vs REPRODUCED (CORE-Bench answer key)")
    print("=" * 78)
    tasks = json.load(open(ANSWER_KEY, encoding="utf-8"))
    task = next(t for t in tasks if "7038571" in str(t["capsule_id"]))
    observed = [float(v) for run_result in task["results"] for v in run_result.values()]
    mean = fmean(observed)
    spec = spec_from_claim(claim, repo_uri=f"local://{os.path.basename(CAPSULE)}")
    tol = spec.claims_under_test[0].tolerance.value
    delta = abs(mean - PAPER_TRUTH["subject_value"])

    print(f"  paper reports (Table 4):   {PAPER_TRUTH['subject_value']}")
    print(f"  capsule actually produces: {[round(v, 4) for v in observed]}")
    print(f"  mean of {len(observed)} runs:          {mean:.4f}")
    print(f"  delta:                     {delta:.4f}")
    print(f"  generated tolerance:       {tol}  (from the claimed margin)")
    print(f"  -> within tolerance? {'yes' if delta <= tol else 'NO'}")
    if delta > tol:
        print("  -> the paper's own number does not reproduce at the policy's bar.")
        print("     A verdict layer must call this RESULT_NEGATIVE, not SUCCESS —")
        print("     and only after the positive control proves the pipeline is sound.")

    # --- 6. what the executor cannot check -----------------------------------
    print("\n" + "=" * 78)
    print("4. SPEC ADAPTED FOR THE EXISTING HARNESS")
    print("=" * 78)
    ut = spec.claims_under_test[0]
    print(f"  experiment_id: {spec.experiment_id}")
    print(f"  comparison:    {ut.comparison}")
    print(f"  seeds:         {ut.seeds}   (from acceptance policy min_seeds)")
    print(f"  control:       {spec.positive_controls[0].metric} = "
          f"{spec.positive_controls[0].expected}")

    passed = sum(results)
    print("\n" + "=" * 78)
    print(f"SCORE: {passed}/{len(results)} field-level checks passed")
    print("=" * 78)


if __name__ == "__main__":
    main()
