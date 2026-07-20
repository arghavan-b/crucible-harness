"""Intake — paper + repo -> Experiment Spec (design §4.1, §6.1).

Two paths:
  - from_paper: the real extraction pipeline. Parse the PDF (text + tables +
    figure images), send it to a vision-capable LLM, get back structured claims
    and baselines with provenance, and map them into an ExperimentSpec — with a
    positive control derived from the paper's own baseline number ("reproduce
    the baseline first").
  - from_repo: offline fallback when there is no paper. Produces a mechanical
    reproduction spec (entry point runs and emits output) plus a generated
    control. Honest about being shallow — real claims need the paper.
"""

from __future__ import annotations

import os

from crucible.planner.analysis import RepoAnalysis, analyze_repo
from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    Tolerance,
)

from .extraction import ExtractedClaim, PaperExtraction
from .llm import LLMClient
from .paper import ParsedPaper, parse_pdf

_HYPOTHESIS_MAP = {
    "comparative": HypothesisType.COMPARATIVE,
    "reproduction": HypothesisType.REPRODUCTION,
    "ablation": HypothesisType.ABLATION,
    "exploratory": HypothesisType.EXPLORATORY,
}


def _experiment_id(repo_uri: str) -> str:
    slug = repo_uri.rstrip("/").split("/")[-1].replace(".git", "") or "repo"
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return f"exp_{slug}"


EXTRACTION_INSTRUCTIONS = """\
You are Crucible's intake. From the paper (text, tables, and figure images) and \
the repository summary, extract the *testable* empirical claims and the baselines \
they are compared against. Focus on numeric results in the results tables.

Return a single JSON object matching this shape:
{
  "title": str,
  "claims": [{
    "claim_id": "c1", "statement": str, "metric": str, "dataset": str|null,
    "method": str, "baseline": str|null,
    "comparison": "method_x > baseline_b"  (use the variable names you also report),
    "reported_value": float|null, "baseline_value": float|null,
    "tolerance": float, "hypothesis_type": "comparative|reproduction|ablation|exploratory",
    "source": {"location": "Table 2, p.5", "quote": str|null}, "confidence": 0.0-1.0
  }],
  "baselines": [{"name": str, "metric": str, "dataset": str|null,
                 "reported_value": float, "source": {"location": str}, "confidence": 0.0-1.0}],
  "datasets": [{"name": str, "url": str|null, "checksum": str|null}],
  "notes": str|null
}
Rules: prefer claims whose numbers appear in a table. Every claim and baseline \
must cite where it came from. Do not invent numbers. If unsure, lower confidence.
"""


class Intake:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

    # --- paper-driven extraction --------------------------------------------

    def from_paper(
        self,
        paper_path: str,
        repo_uri: str,
        root: str,
        *,
        max_figures: int = 6,
    ) -> tuple[ExperimentSpec, PaperExtraction, RepoAnalysis]:
        if self.llm is None:
            raise RuntimeError("from_paper requires an LLM client (Intake(llm=...)).")
        paper = parse_pdf(paper_path)
        analysis = analyze_repo(root)
        prompt = self._build_prompt(paper, analysis)
        images = [(f.media_type, f.image_b64) for f in paper.figures[:max_figures]]
        raw = self.llm.complete_json(prompt, images=images)
        extraction = PaperExtraction.model_validate(raw)
        spec = self._spec_from_extraction(repo_uri, extraction)
        return spec, extraction, analysis

    def _build_prompt(self, paper: ParsedPaper, analysis: RepoAnalysis) -> str:
        text = paper.full_text[:24000]
        tables = paper.tables_markdown()[:12000]
        repo = (
            f"entry_points={analysis.entry_points}, manifests={analysis.dependency_manifests}, "
            f"packages={analysis.top_level_packages}, readme_present={bool(analysis.readme)}"
        )
        return (
            f"{EXTRACTION_INSTRUCTIONS}\n\n"
            f"=== REPOSITORY ===\n{repo}\n\n"
            f"=== PAPER TABLES ===\n{tables}\n\n"
            f"=== PAPER TEXT ===\n{text}\n"
        )

    def _spec_from_extraction(self, repo_uri: str, ex: PaperExtraction) -> ExperimentSpec:
        claims = [self._claim(c) for c in ex.claims] or [self._fallback_claim()]
        primary_type = _HYPOTHESIS_MAP.get(
            (ex.claims[0].hypothesis_type if ex.claims else "reproduction").lower(),
            HypothesisType.REPRODUCTION,
        )
        controls = self._controls_from_extraction(ex)
        return ExperimentSpec(
            experiment_id=_experiment_id(repo_uri),
            hypothesis=Hypothesis(
                statement=(ex.claims[0].statement if ex.claims else "Reproduce the paper's result."),
                type=primary_type,
            ),
            source=Source(repo_uri=repo_uri, commit=None),
            claims_under_test=claims,
            positive_controls=controls,
        )

    def _claim(self, c: ExtractedClaim) -> ClaimUnderTest:
        reported: dict[str, float] = {}
        if c.method and c.reported_value is not None:
            reported[c.method] = c.reported_value
        if c.baseline and c.baseline_value is not None:
            reported[c.baseline] = c.baseline_value
        return ClaimUnderTest(
            claim_id=c.claim_id,
            metric=c.metric,
            comparison=c.comparison,
            reported_values=reported,
            tolerance=Tolerance(value=c.tolerance),
            seeds=[0, 1, 2],
        )

    def _controls_from_extraction(self, ex: PaperExtraction) -> list[PositiveControl]:
        # Reproduce the paper's own baseline number first — the key control.
        if ex.baselines:
            b = ex.baselines[0]
            return [
                PositiveControl(
                    control_id="pc1",
                    description=f"Reproduce reported baseline: {b.name} = {b.reported_value}",
                    metric=b.metric,
                    expected=b.reported_value,
                    tolerance=Tolerance(value=0.01),
                )
            ]
        # Fall back to a claim's baseline_value if no explicit baseline was extracted.
        for c in ex.claims:
            if c.baseline and c.baseline_value is not None:
                return [
                    PositiveControl(
                        control_id="pc1",
                        description=f"Reproduce reported baseline: {c.baseline} = {c.baseline_value}",
                        metric=c.metric,
                        expected=c.baseline_value,
                        tolerance=Tolerance(value=0.01),
                    )
                ]
        return [self._mechanical_control()]

    # --- repo-only fallback --------------------------------------------------

    def prepare(
        self, repo_uri: str, root: str, paper_uri: str | None = None
    ) -> tuple[ExperimentSpec, RepoAnalysis]:
        analysis = analyze_repo(root)
        spec = ExperimentSpec(
            experiment_id=_experiment_id(repo_uri),
            hypothesis=Hypothesis(
                statement=f"The project at {os.path.basename(root)} runs and produces output.",
                type=HypothesisType.REPRODUCTION,
            ),
            source=Source(repo_uri=repo_uri, commit=None),
            claims_under_test=[self._fallback_claim()],
            positive_controls=[self._mechanical_control()],
        )
        return spec, analysis

    def from_repo(self, repo_uri: str, root: str, paper_uri: str | None = None) -> ExperimentSpec:
        return self.prepare(repo_uri, root, paper_uri)[0]

    def _fallback_claim(self) -> ClaimUnderTest:
        return ClaimUnderTest(
            claim_id="c1",
            metric="output_artifact_produced",
            comparison="output_artifact_produced >= 1",
            reported_values={"output_artifact_produced": 1.0},
            tolerance=Tolerance(value=0.0),
            seeds=[0],
        )

    def _mechanical_control(self) -> PositiveControl:
        return PositiveControl(
            control_id="pc1",
            description="entry point runs to completion and exits cleanly",
            metric="smoke_exit_code",
            expected=0.0,
            tolerance=Tolerance(value=0.0),
        )

    def generate_positive_control(self, analysis: RepoAnalysis) -> PositiveControl:
        return self._mechanical_control()
