"""Claim intake — paper and/or repo -> typed Claims with policies and artifacts.

Three input shapes, all landing on the same ClaimIntakeResult:

  - paper + repo : the full path. Compile the repo first so the extractor sees
    what artifacts exist, extract claims from the paper, then attach policies.
  - paper only   : extract and attach policies; artifact report is empty and
    every claim is capped at INCONCLUSIVE(artifacts_unavailable).
  - repo only    : no claims can be extracted (a repo asserts nothing), but the
    artifact report and auditability score are still produced — which is the
    useful answer to "could this repo ever be audited?".

Order matters: the repo is compiled *before* extraction so the paper prompt
carries the repo's real structure, and so a submission that cannot be audited is
identified before any model call is paid for.

"Report" covers .md/.txt/.pdf — a report is just a paper the heuristic path can
read directly as text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from crucible.intake.llm import LLMClient

from .compiler import ArtifactReport, compile_procedure, repo_summary
from .extract import HeuristicExtractor, LLMExtractor
from .policy import ensure_policies
from .schema import AcceptancePolicy, Claim, ClaimSet

_TEXT_EXTS = (".md", ".txt", ".rst")


@dataclass
class ClaimIntakeResult:
    claim_set: ClaimSet
    artifacts: ArtifactReport | None = None
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def claims(self) -> list[Claim]:
        return self.claim_set.claims

    @property
    def auditability(self) -> float:
        return self.artifacts.auditability_score if self.artifacts else 0.0

    def adjudicable(self) -> list[Claim]:
        """Claims that could get a verdict — empty if artifacts block the run."""
        if self.blocked_reason:
            return []
        return self.claim_set.adjudicable()


class ClaimIntake:
    """Orchestrates extraction, policy generation, and repo compilation.

    `llm=None` selects the offline heuristic extractor, so the whole path runs
    with no API key. The policy is always generated deterministically — never by
    the model — because the policy is the bar the verdict is measured against.
    """

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

    def ingest(
        self,
        paper: str | None = None,
        repo: str | None = None,
        policy: AcceptancePolicy | None = None,
    ) -> ClaimIntakeResult:
        if paper is None and repo is None:
            raise ValueError("claim intake needs a paper, a repo, or both")

        warnings: list[str] = []
        artifacts: ArtifactReport | None = None
        summary: str | None = None
        if repo is not None:
            artifacts = compile_procedure(repo)
            summary = repo_summary(artifacts)

        if paper is not None:
            claim_set = self._extract(paper, summary)
        else:
            claim_set = ClaimSet(
                notes="no paper or report supplied — repo compiled for auditability only"
            )
            warnings.append("no paper/report: claims cannot be extracted from a repo alone")

        # Policies: an explicitly supplied policy is authored and wins; otherwise
        # intake generates a domain default per claim ("no policy, no verdict").
        if policy is not None:
            claim_set.claims = [
                c.model_copy(update={"acceptance_policy": policy}) for c in claim_set.claims
            ]
        else:
            claim_set.claims = ensure_policies(claim_set.claims)

        claim_set.repo_root = os.path.abspath(repo) if repo else None

        blocked = artifacts.blocking_reason() if artifacts else "artifacts_unavailable"
        if blocked and repo is None:
            warnings.append("no repo supplied: nothing to check the claim's split against")

        for claim, reason in claim_set.blocked():
            warnings.append(f"{claim.claim_id}: not adjudicable ({reason})")

        return ClaimIntakeResult(
            claim_set=claim_set,
            artifacts=artifacts,
            blocked_reason=blocked,
            warnings=warnings,
        )

    def _extract(self, paper: str, summary: str | None) -> ClaimSet:
        if os.path.splitext(paper)[1].lower() in _TEXT_EXTS:
            # A markdown/text report needs no PDF parsing; the LLM path still
            # applies if a client was supplied.
            with open(paper, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            if self.llm is not None:
                return self._llm_from_text(text, summary, paper)
            claim_set = HeuristicExtractor().from_text(text, paper_path=paper)
            return claim_set
        if self.llm is not None:
            return LLMExtractor(self.llm).extract(paper, repo_summary=summary)
        return HeuristicExtractor().extract(paper, repo_summary=summary)

    def _llm_from_text(self, text: str, summary: str | None, paper: str) -> ClaimSet:
        from .extract import EXTRACTION_INSTRUCTIONS

        prompt = (
            f"{EXTRACTION_INSTRUCTIONS}\n\n"
            f"=== REPOSITORY ===\n{summary or '(no repo provided)'}\n\n"
            f"=== REPORT ===\n{text[:36000]}\n"
        )
        raw = self.llm.complete_json(prompt)  # type: ignore[union-attr]
        claim_set = ClaimSet.model_validate(
            {
                "title": raw.get("title"),
                "claims": raw.get("claims") or [],
                "datasets": raw.get("datasets") or [],
                "notes": raw.get("notes"),
            }
        )
        claim_set.paper_path = paper
        return claim_set
