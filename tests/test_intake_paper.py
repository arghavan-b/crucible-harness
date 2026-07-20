"""Paper parsing + LLM-driven extraction tests (design §6.1)."""

from __future__ import annotations

import pytest

from crucible.intake import FakeClient, Intake, parse_pdf
from crucible.schemas import HypothesisType, VerdictStatus


def _make_pdf(path: str) -> None:
    """Render a tiny paper with a results table using reportlab."""
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter)
    text = c.beginText(72, 720)
    for line in [
        "Method X: Better Image Classification",
        "Abstract: Method X improves top-1 accuracy over the ResNet-50 baseline.",
        "",
        "Table 2: Results on ImageNet",
        "Method        Top-1",
        "ResNet-50     81.2",
        "Method X      84.7",
    ]:
        text.textLine(line)
    c.drawText(text)
    c.showPage()
    c.save()


# --- PDF parsing --------------------------------------------------------------


def test_parse_pdf_extracts_text(tmp_path) -> None:
    pytest.importorskip("pdfplumber")
    pdf = str(tmp_path / "paper.pdf")
    _make_pdf(pdf)
    parsed = parse_pdf(pdf, include_figures=False)
    assert parsed.page_text
    assert "Method X" in parsed.full_text
    assert "81.2" in parsed.full_text


# --- extraction pipeline (fake LLM) ------------------------------------------


CANNED_EXTRACTION = {
    "title": "Method X: Better Image Classification",
    "claims": [
        {
            "claim_id": "c1",
            "statement": "Method X improves top-1 accuracy over ResNet-50 on ImageNet.",
            "metric": "top1_accuracy",
            "dataset": "ImageNet",
            "method": "method_x",
            "baseline": "resnet50",
            "comparison": "method_x > resnet50",
            "reported_value": 84.7,
            "baseline_value": 81.2,
            "tolerance": 0.5,
            "hypothesis_type": "comparative",
            "source": {"location": "Table 2, p.1", "quote": "Method X 84.7"},
            "confidence": 0.9,
        }
    ],
    "baselines": [
        {
            "name": "ResNet-50",
            "metric": "top1_accuracy",
            "dataset": "ImageNet",
            "reported_value": 81.2,
            "source": {"location": "Table 2, p.1"},
            "confidence": 0.95,
        }
    ],
    "datasets": [{"name": "ImageNet", "url": None, "checksum": None}],
    "notes": None,
}


def test_from_paper_builds_spec_from_extraction(tmp_path) -> None:
    pytest.importorskip("pdfplumber")
    pdf = str(tmp_path / "paper.pdf")
    _make_pdf(pdf)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("if __name__ == '__main__':\n    pass\n")

    fake = FakeClient([CANNED_EXTRACTION])
    spec, extraction, analysis = Intake(llm=fake).from_paper(
        pdf, repo_uri="github.com/author/method-x", root=str(repo)
    )

    # The claim came from the paper, not a hardcoded stub.
    assert spec.hypothesis.type is HypothesisType.COMPARATIVE
    claim = spec.claims_under_test[0]
    assert claim.comparison == "method_x > resnet50"
    assert claim.reported_values == {"method_x": 84.7, "resnet50": 81.2}

    # The positive control reproduces the paper's own baseline number.
    pc = spec.positive_controls[0]
    assert pc.expected == 81.2
    assert "baseline" in pc.description.lower()

    # Provenance survived into the extraction object.
    assert extraction.claims[0].source.location == "Table 2, p.1"
    assert extraction.claims[0].confidence == 0.9


def test_from_paper_passes_figures_to_client(tmp_path) -> None:
    pytest.importorskip("pdfplumber")
    pdf = str(tmp_path / "paper.pdf")
    _make_pdf(pdf)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("if __name__ == '__main__':\n    pass\n")
    fake = FakeClient([CANNED_EXTRACTION])
    Intake(llm=fake).from_paper(pdf, repo_uri="x", root=str(repo))
    # The client was called once; the prompt mentions the tables section.
    assert len(fake.calls) == 1
    assert "PAPER TABLES" in fake.calls[0][0]


def test_from_paper_requires_llm(tmp_path) -> None:
    pdf = str(tmp_path / "p.pdf")
    _make_pdf(pdf)
    with pytest.raises(RuntimeError):
        Intake().from_paper(pdf, repo_uri="x", root=str(tmp_path))
