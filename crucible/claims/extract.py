"""Claim extraction from a paper or report (domain design §2, master §6.1).

Two paths, mirroring the rest of intake:

  - LLMExtractor: parse the PDF (text + results tables + figure images), hand it
    to a vision-capable model, and get back typed Claims with provenance and
    confidence. The model fills *fields*; it never decides what counts as valid
    — the acceptance policy does that, and it is generated deterministically.
  - HeuristicExtractor: offline fallback with no API key. Reads results tables
    and comparative sentences well enough to produce a reviewable draft claim,
    honestly marked low-confidence. It exists so the whole pipeline (and the
    test suite) runs with no network.

Both emit the same ClaimSet, so downstream code never branches on which ran.

The extractor is deliberately conservative about numbers: a claim whose value is
not present in the source is left None rather than guessed, because a fabricated
reported value produces a false verdict later — the cardinal failure.
"""

from __future__ import annotations

import re
from typing import Protocol

from crucible.intake.llm import LLMClient
from crucible.intake.paper import ParsedPaper, parse_pdf

from .schema import (
    AssayType,
    Claim,
    ClaimContext,
    ClaimSet,
    ClaimType,
    DatasetRef,
    Margin,
    Relation,
    ReportedValues,
    Representation,
    SourceRef,
    SplitMethod,
    SplitSpec,
    Statement,
)

# --- text signals used by the heuristic path ---------------------------------

_METRIC_TOKENS = (
    "auroc", "auc-roc", "roc-auc", "auc", "auprc", "aupr", "prc-auc", "average precision",
    "accuracy", "f1", "mcc", "rmse", "mae", "r2", "spearman", "pearson", "kappa",
    "balanced accuracy", "precision", "recall",
)
_COMPARATIVE_CUES = (
    "outperform", "outperforms", "outperformed", "improves over", "improvement over",
    "better than", "surpass", "surpasses", "exceeds", "superior to", "state-of-the-art",
    "achieves the best", "beats",
)
_SPLIT_PATTERNS: list[tuple[re.Pattern[str], SplitMethod]] = [
    (re.compile(r"\bscaffold[\s-]?split|bemis[\s-]?murcko", re.I), SplitMethod.SCAFFOLD),
    (re.compile(r"\btemporal split|time[\s-]?split|time[\s-]?based split", re.I),
     SplitMethod.TEMPORAL),
    (re.compile(r"\bcluster split|cluster[\s-]?based split", re.I), SplitMethod.CLUSTER),
    (re.compile(r"\bpredefined split|official split|provided split", re.I),
     SplitMethod.PREDEFINED),
    (re.compile(r"\brandom split|randomly split", re.I), SplitMethod.RANDOM),
]
_RATIO_RE = re.compile(r"\b(\d{1,2})\s*[:/]\s*(\d{1,2})\s*[:/]\s*(\d{1,2})\b")
_SEED_RE = re.compile(r"\bseed(?:s)?\s*(?:=|:|\s)\s*(\d{1,5})\b", re.I)
_FEATURIZER_RE = re.compile(
    r"\b(morgan|ecfp\d?|fcfp\d?|maccs|rdkit[\s_-]?desc\w*|mordred|graph|smiles|"
    r"one[\s-]?hot|fingerprint)\w*", re.I
)
_DATASET_RE = re.compile(
    r"\b(TDC|MoleculeNet|ChEMBL|BBBP|Tox21|SIDER|ClinTox|HIV|BACE|ESOL|FreeSolv|Lipophilicity|"
    r"QM\d|PCBA|MUV|Delaney)\b[\w./-]*"
)
_VARIANCE_RE = re.compile(r"(±|\+/-|\bstd\b|\bstandard deviation\b|\bconfidence interval\b|\bCI\b)")

_ASSAY_HINTS: list[tuple[re.Pattern[str], AssayType]] = [
    (re.compile(r"\bauroc|auc|auprc|classif|binary|inhibit(or|ion)\b", re.I),
     AssayType.BINARY_CLASSIFICATION),
    (re.compile(r"\brmse|mae|r2|regress|solubility|logd|logp|clearance\b", re.I),
     AssayType.REGRESSION),
]


class Extractor(Protocol):
    def extract(self, paper_path: str, repo_summary: str | None = None) -> ClaimSet: ...


# --- LLM path -----------------------------------------------------------------

EXTRACTION_INSTRUCTIONS = """\
You are Crucible's claim extractor for molecular-property / ADMET / bioactivity
prediction papers. From the paper (text, results tables, figure images) and the
repository summary, extract the *testable quantitative* claims.

Return a single JSON object:
{
  "title": str|null,
  "claims": [{
    "claim_id": "claim-001",
    "type": "comparative_performance|reproduction|absolute|ranking|ablation",
    "statement": {
      "subject": str,                  // the method being claimed for
      "relation": "outperforms|achieves|ranks_above|is_robust_to",
      "comparator": str|null,          // the baseline it is compared against
      "margin": {"metric": str, "delta": float|null},
      "text": str|null                 // the verbatim claim sentence
    },
    "context": {
      "endpoint": str|null,            // e.g. "CYP2D6_inhibition"
      "assay_type": "binary_classification|multiclass_classification|regression|ranking|unknown",
      "dataset": {"name": str, "version": str|null, "hash": null, "url": str|null},
      "split": {"method": "random|scaffold|temporal|cluster|predefined|unknown",
                "ratio": [float], "seed": int|null, "date_field": str|null},
      "representation": {"featurizer": str|null, "notes": str|null}
    },
    "reported": {
      "subject_value": float|null, "comparator_value": float|null, "metric": str|null,
      "per_seed": {str: [float]}, "variance_reported": bool
    },
    "source": {"location": "Table 2, p.5", "quote": str|null},
    "confidence": 0.0-1.0
  }],
  "datasets": [{"name": str, "version": str|null, "url": str|null}],
  "notes": str|null
}

Rules:
- Prefer claims whose numbers appear in a results table.
- Report the split method ONLY if the paper states it. If it is not stated, use
  "unknown" — do not infer it from the dataset's convention.
- Never invent a number. If a value is not in the source, use null and lower
  confidence. A fabricated value becomes a false verdict downstream.
- Every claim must cite where it came from.
- Do NOT decide whether the claim is valid, and do not emit an acceptance
  policy. Your job is only to say what was claimed and on what.
"""


def _build_prompt(paper: ParsedPaper, repo_summary: str | None) -> str:
    return (
        f"{EXTRACTION_INSTRUCTIONS}\n\n"
        f"=== REPOSITORY ===\n{repo_summary or '(no repo provided)'}\n\n"
        f"=== PAPER TABLES ===\n{paper.tables_markdown()[:12000]}\n\n"
        f"=== PAPER TEXT ===\n{paper.full_text[:24000]}\n"
    )


class LLMExtractor:
    """Model-backed extraction. The model fills typed fields; the harness owns
    the schema, the policy, and every downstream decision."""

    def __init__(self, llm: LLMClient, max_figures: int = 6) -> None:
        self.llm = llm
        self.max_figures = max_figures

    def extract(self, paper_path: str, repo_summary: str | None = None) -> ClaimSet:
        paper = parse_pdf(paper_path)
        images = [(f.media_type, f.image_b64) for f in paper.figures[: self.max_figures]]
        raw = self.llm.complete_json(_build_prompt(paper, repo_summary), images=images)
        claim_set = ClaimSet.model_validate(
            {
                "title": raw.get("title"),
                "claims": raw.get("claims") or [],
                "datasets": raw.get("datasets") or [],
                "notes": raw.get("notes"),
            }
        )
        claim_set.paper_path = paper_path
        return claim_set


# --- offline heuristic path ---------------------------------------------------


def _first_metric(text: str) -> str | None:
    low = text.lower()
    hits = [(low.find(m), m) for m in _METRIC_TOKENS if m in low]
    if not hits:
        return None
    return min(hits)[1].upper()


def _detect_split(text: str) -> SplitSpec:
    method = SplitMethod.UNKNOWN
    for pattern, candidate in _SPLIT_PATTERNS:
        if pattern.search(text):
            method = candidate
            break
    ratio: list[float] = []
    m = _RATIO_RE.search(text)
    if m:
        nums = [float(g) for g in m.groups()]
        total = sum(nums)
        if total > 0:
            ratio = [round(n / total, 3) for n in nums]
    seed_match = _SEED_RE.search(text)
    seed = int(seed_match.group(1)) if seed_match else None
    return SplitSpec(method=method, ratio=ratio, seed=seed)


def _detect_assay(text: str) -> AssayType:
    for pattern, assay in _ASSAY_HINTS:
        if pattern.search(text):
            return assay
    return AssayType.UNKNOWN


def _detect_datasets(text: str) -> list[DatasetRef]:
    names: list[str] = []
    for m in _DATASET_RE.finditer(text):
        name = m.group(0).strip(".,;)")
        if name not in names:
            names.append(name)
    return [DatasetRef(name=n) for n in names[:10]]


def _claim_sentences(text: str, limit: int = 6) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    for s in sentences:
        low = s.lower()
        if any(cue in low for cue in _COMPARATIVE_CUES) and len(s) < 400:
            out.append(" ".join(s.split()))
            if len(out) >= limit:
                break
    return out


def _numbers_in(sentence: str) -> list[float]:
    vals: list[float] = []
    for tok in re.findall(r"\b\d+\.\d+\b|\b0?\.\d+\b", sentence):
        try:
            vals.append(float(tok))
        except ValueError:
            continue
    return vals


class HeuristicExtractor:
    """Offline extraction — no API key, no network.

    Deliberately shallow and honest about it: it finds comparative sentences and
    the split/dataset/metric context around them, and marks everything
    low-confidence. Its purpose is (a) to keep the pipeline runnable offline and
    (b) to give a human a structured draft to correct, not to be trusted.
    """

    def __init__(self, max_claims: int = 5) -> None:
        self.max_claims = max_claims

    def extract(self, paper_path: str, repo_summary: str | None = None) -> ClaimSet:
        paper = parse_pdf(paper_path)
        text = paper.full_text
        tables = paper.tables_markdown()
        return self.from_text(
            text, tables=tables, paper_path=paper_path, title=self._title(text)
        )

    def _title(self, text: str) -> str | None:
        for line in text.splitlines():
            clean = line.strip()
            if clean.startswith("[page"):
                continue
            if len(clean) > 15:
                return clean[:200]
        return None

    def from_text(
        self,
        text: str,
        tables: str = "",
        paper_path: str | None = None,
        title: str | None = None,
    ) -> ClaimSet:
        """Extract from raw text — used directly for .md/.txt reports, and by
        `extract()` after the PDF is parsed."""
        haystack = f"{text}\n{tables}"
        split = _detect_split(haystack)
        assay = _detect_assay(haystack)
        datasets = _detect_datasets(haystack)
        featurizer_match = _FEATURIZER_RE.search(haystack)
        representation = Representation(
            featurizer=featurizer_match.group(0) if featurizer_match else None
        )
        variance_reported = bool(_VARIANCE_RE.search(haystack))

        claims: list[Claim] = []
        for i, sentence in enumerate(_claim_sentences(haystack, self.max_claims), start=1):
            metric = _first_metric(sentence) or _first_metric(haystack)
            values = _numbers_in(sentence)
            subject_value = values[0] if values else None
            comparator_value = values[1] if len(values) > 1 else None
            claims.append(
                Claim(
                    claim_id=f"claim-{i:03d}",
                    type=ClaimType.COMPARATIVE,
                    statement=Statement(
                        subject="(unresolved: name the method)",
                        relation=Relation.OUTPERFORMS,
                        comparator=None,
                        margin=Margin(metric=metric) if metric else None,
                        text=sentence,
                    ),
                    context=ClaimContext(
                        endpoint=datasets[0].name if datasets else None,
                        assay_type=assay,
                        dataset=datasets[0] if datasets else None,
                        split=split,
                        representation=representation,
                    ),
                    reported=ReportedValues(
                        subject_value=subject_value,
                        comparator_value=comparator_value,
                        metric=metric,
                        variance_reported=variance_reported,
                    ),
                    source=SourceRef(location="heuristic: comparative sentence", quote=sentence),
                    confidence=0.25,
                    notes="offline heuristic draft — subject/comparator need human or LLM review",
                )
            )

        return ClaimSet(
            title=title,
            claims=claims,
            datasets=datasets,
            paper_path=paper_path,
            notes=(
                None if claims
                else "no comparative claim sentence found; supply the claim manually or use an LLM"
            ),
        )
