"""Adapter for real CORE-Bench data (design §12.2).

CORE-Bench ships task metadata (`core_train.json` / `core_test.json`) separately
from the code capsules. Each task has: field, language, capsule_title,
capsule_id, task_prompt, results, capsule_doi. The `results` list is the answer
key — one dict of {question: value} per run/seed. Capsule code is downloaded by
id from Princeton.

This maps a task into a Crucible ExperimentSpec: each distinct question becomes a
reproduction claim whose reported value is the mean of its runs and whose
tolerance covers their spread; a mechanical positive control (the entry runs and
exits cleanly) stands in because CORE-Bench tasks carry no separate baseline
number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from statistics import fmean

from crucible.schemas import (
    ClaimUnderTest,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PositiveControl,
    Source,
    Tolerance,
)

CAPSULE_BASE = "https://corebench.cs.princeton.edu/capsules"


@dataclass
class CoreBenchTask:
    capsule_id: str
    capsule_title: str
    field: str
    language: str
    task_prompt: str
    results: list[dict[str, object]] = field(default_factory=list)
    capsule_doi: str | None = None

    @property
    def capsule_url(self) -> str:
        cid = self.capsule_id if self.capsule_id.startswith("capsule-") else f"capsule-{self.capsule_id}"
        return f"{CAPSULE_BASE}/{cid}.tar.gz"


def load_core_bench(path: str) -> list[CoreBenchTask]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        CoreBenchTask(
            capsule_id=str(t["capsule_id"]),
            capsule_title=t.get("capsule_title", ""),
            field=t.get("field", ""),
            language=t.get("language", ""),
            task_prompt=t.get("task_prompt", ""),
            results=t.get("results", []),
            capsule_doi=t.get("capsule_doi"),
        )
        for t in raw
    ]


def find(tasks: list[CoreBenchTask], capsule_id: str) -> CoreBenchTask:
    key = capsule_id.replace("capsule-", "")
    for t in tasks:
        if t.capsule_id.replace("capsule-", "") == key:
            return t
    raise KeyError(f"capsule {capsule_id} not found")


def _slug(question: str, i: int) -> str:
    words = re.findall(r"[a-z0-9]+", question.lower())
    stop = {"the", "of", "a", "on", "using", "and", "report", "score", "method", "dataset"}
    keep = [w for w in words if w not in stop][:4]
    return "_".join(keep) or f"answer_{i}"


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def to_spec(task: CoreBenchTask, repo_uri: str | None = None) -> ExperimentSpec:
    # Group the answer key by question across runs/seeds.
    by_question: dict[str, list[float]] = {}
    for run in task.results:
        for question, value in run.items():
            num = _numeric(value)
            if num is not None:
                by_question.setdefault(question, []).append(num)

    claims: list[ClaimUnderTest] = []
    for i, (question, values) in enumerate(by_question.items(), start=1):
        slug = _slug(question, i)
        reported = fmean(values)
        spread = (max(values) - min(values)) if len(values) > 1 else 0.0
        claims.append(ClaimUnderTest(
            claim_id=f"c{i}",
            metric=slug,
            comparison=f"{slug} ~= {reported:.6g}",
            reported_values={slug: reported},
            tolerance=Tolerance(value=max(0.01, spread)),
            seeds=list(range(max(1, len(values)))),
        ))
    if not claims:
        claims.append(ClaimUnderTest(
            claim_id="c1", metric="output_artifact_produced",
            comparison="output_artifact_produced >= 1",
            reported_values={"output_artifact_produced": 1.0},
            tolerance=Tolerance(value=0.0), seeds=[0],
        ))

    uri = repo_uri or f"corebench://{task.capsule_id}"
    return ExperimentSpec(
        experiment_id=f"exp_{task.capsule_id.replace('-', '_')}",
        hypothesis=Hypothesis(
            statement=task.task_prompt or task.capsule_title,
            type=HypothesisType.REPRODUCTION,
        ),
        source=Source(repo_uri=uri, commit=None),
        claims_under_test=claims,
        # CORE-Bench has no separate baseline number; use a mechanical control.
        positive_controls=[PositiveControl(
            control_id="pc1",
            description="entry point runs to completion and exits cleanly",
            metric="smoke_exit_code", expected=0.0, tolerance=Tolerance(value=0.0),
        )],
    )
