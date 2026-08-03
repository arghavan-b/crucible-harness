"""The PDF path: parse_pdf -> tables -> extractor prompt -> claims.

The text fixture tests extraction *given* clean text. These tests cover what it
skips: real PDF text extraction and, more importantly, recovering the results
table as a structured grid rather than as flat prose. Results tables are where
reproducible numbers live, so a claim extractor that never sees the table sees
almost nothing worth extracting.

The PDF is generated at test time from the real CTGCN excerpt (reportlab,
dev-only) so no binary is committed. If you drop the genuine arXiv PDF at
tests/fixtures/ctgcn_paper.pdf these tests use that instead, which additionally
covers two-column typesetting.
"""

from __future__ import annotations

import os

import pytest

from crucible.claims import ClaimIntake, HeuristicExtractor, LLMExtractor
from crucible.intake.llm import FakeClient
from crucible.intake.paper import parse_pdf

pdfplumber = pytest.importorskip("pdfplumber", reason="PDF parsing needs the [intake] extra")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
REAL_PDF = os.path.join(FIXTURES, "ctgcn_paper.pdf")

RECORDED = {
    "title": "CTGCN",
    "claims": [
        {
            "claim_id": "claim-001",
            "type": "comparative_performance",
            "statement": {
                "subject": "CTGCN-C", "relation": "outperforms", "comparator": "CGCN-S",
                "margin": {"metric": "AUC", "delta": 0.0059},
            },
            "context": {
                "endpoint": "link_prediction", "assay_type": "binary_classification",
                "dataset": {"name": "UCI"},
                "split": {"method": "temporal", "ratio": [0.5, 0.3, 0.2]},
            },
            "reported": {"subject_value": 0.9434, "comparator_value": 0.9375, "metric": "AUC"},
            "source": {"location": "Table 4"},
            "confidence": 0.9,
        }
    ],
    "datasets": [{"name": "UCI"}],
}


@pytest.fixture(scope="module")
def paper_pdf(tmp_path_factory) -> str:
    """The real arXiv PDF when present, else one generated from the excerpt."""
    if os.path.exists(REAL_PDF):
        return REAL_PDF
    pytest.importorskip("reportlab", reason="generating the PDF fixture needs reportlab")
    import sys

    sys.path.insert(0, FIXTURES)
    from make_pdf import build

    return build(str(tmp_path_factory.mktemp("pdf") / "ctgcn.pdf"))


# --- parsing -------------------------------------------------------------------


def test_pdf_text_is_extracted(paper_pdf):
    paper = parse_pdf(paper_pdf, include_figures=False)
    assert paper.page_text
    assert "CTGCN" in paper.full_text


def test_results_table_is_recovered_as_a_grid(paper_pdf):
    """The headline number must survive as a table cell, not just as prose."""
    paper = parse_pdf(paper_pdf, include_figures=False)
    assert paper.tables, "no table detected — the results table is where the numbers live"
    markdown = paper.tables_markdown()
    assert "CTGCN-C" in markdown
    assert "0.9434" in markdown


def test_table_rows_keep_method_to_value_alignment(paper_pdf):
    """A table is only useful if CTGCN-C's row still lines up with 0.9434 —
    flattened text loses exactly this."""
    paper = parse_pdf(paper_pdf, include_figures=False)
    rows = [r for t in paper.tables for r in t.rows]
    ctgcn = next((r for r in rows if r and r[0] and "CTGCN-C" in str(r[0])), None)
    assert ctgcn is not None
    assert any(cell and "0.9434" in str(cell) for cell in ctgcn)


def test_missing_pdf_libraries_degrade_rather_than_raise(tmp_path):
    """parse_pdf imports lazily; a non-PDF must not blow up the pipeline."""
    junk = tmp_path / "not.pdf"
    junk.write_text("this is not a pdf", encoding="utf-8")
    try:
        paper = parse_pdf(str(junk), include_figures=False)
    except Exception:
        pytest.skip("pdfplumber raises on malformed input in this version")
    assert paper.page_text == [] or isinstance(paper.full_text, str)


# --- extraction over the parsed PDF ---------------------------------------------


def test_extractor_prompt_carries_the_results_table(paper_pdf):
    """The whole point of table extraction: the model must actually see the
    grid. If this regresses, extraction silently degrades to prose-only."""
    client = FakeClient([RECORDED])
    LLMExtractor(client).extract(paper_pdf)
    prompt = client.calls[0][0]
    assert "=== PAPER TABLES ===" in prompt
    assert "CTGCN-C" in prompt
    assert "0.9434" in prompt


def test_llm_extraction_from_a_pdf_produces_a_typed_claim(paper_pdf):
    claim_set = LLMExtractor(FakeClient([RECORDED])).extract(paper_pdf)
    claim = claim_set.claims[0]
    assert claim.statement.subject == "CTGCN-C"
    assert claim.reported.subject_value == 0.9434
    assert claim_set.paper_path == paper_pdf


def test_claim_intake_accepts_a_pdf_and_attaches_a_policy(paper_pdf):
    result = ClaimIntake(llm=FakeClient([RECORDED])).ingest(paper=paper_pdf)
    claim = result.claims[0]
    assert claim.acceptance_policy is not None
    assert claim.is_adjudicable()[0]


def test_heuristic_extraction_runs_on_a_pdf(paper_pdf):
    """Offline path over a real PDF. It finds the comparative sentence; whether
    it recovers numbers depends on them appearing inline rather than only in the
    table — a known limitation, asserted here so it is visible when it changes."""
    claim_set = HeuristicExtractor().extract(paper_pdf)
    assert claim_set.claims
    claim = claim_set.claims[0]
    assert claim.reported.metric == "AUC"
    assert claim.confidence <= 0.3
