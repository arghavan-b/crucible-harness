"""LLM calls are recorded into the trace / certificate (design §6.5)."""

from __future__ import annotations

import pytest

from crucible.intake import FakeClient, LoggingLLMClient
from crucible.pipeline import run_pipeline
from crucible.trace.recorder import SQLiteTraceRecorder


def test_logging_client_records_call(tmp_path) -> None:
    rec = SQLiteTraceRecorder(str(tmp_path / "t.sqlite"))
    tid = rec.start("exp")
    client = LoggingLLMClient(FakeClient([{"ok": True}]), rec, tid, "intake")
    out = client.complete_json("hello", images=[("image/png", "abc")])
    assert out == {"ok": True}
    calls = [e for e in rec.events(tid) if e["kind"] == "llm_call"]
    assert len(calls) == 1
    assert calls[0]["payload"]["role"] == "intake"
    assert calls[0]["payload"]["output"] == {"ok": True}


CANNED = {
    "title": "X", "claims": [{
        "claim_id": "c1", "statement": "s", "metric": "accuracy", "method": "m",
        "comparison": "accuracy >= 1", "reported_value": 1.0, "tolerance": 0.0,
        "hypothesis_type": "reproduction", "source": {"location": "Table 1"}, "confidence": 0.9,
    }], "baselines": [], "datasets": [],
}


def test_pipeline_trace_includes_llm_calls(tmp_path) -> None:
    pytest.importorskip("pdfplumber")
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas

    pdf = str(tmp_path / "paper.pdf")
    c = canvas.Canvas(pdf)
    c.drawString(72, 720, "Table 1: accuracy 1.0")
    c.showPage()
    c.save()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "inference.py").write_text(
        "import json, os\n"
        "if __name__ == '__main__':\n"
        "    os.makedirs('outputs', exist_ok=True)\n"
        "    json.dump({'accuracy': 1.0}, open('outputs/metrics.json', 'w'))\n"
    )

    rec = SQLiteTraceRecorder(str(tmp_path / "trace.sqlite"))
    fake = FakeClient([CANNED, {"bindings": []}])  # extraction, then grounding
    result = run_pipeline(str(repo), paper=pdf, llm=fake, recorder=rec)

    events = rec.events(result.certificate.trace_id)
    roles = {e["payload"].get("role") for e in events if e["kind"] == "llm_call"}
    assert "intake" in roles
    assert "grounding" in roles
    # The trace is unified: execution events share it with the LLM calls.
    assert any(e["kind"] == "run_finished" for e in events)
