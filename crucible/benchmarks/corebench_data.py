"""Adapter for real CORE-Bench data (design §12.2).

CORE-Bench ships task metadata (`core_train.json` / `core_test.json`) separately
from the code capsules. Each task has: field, language, capsule_title,
capsule_id, task_prompt, results, capsule_doi. The `results` list is the answer
key — one dict of {question: value} per run/seed. Capsule code is downloaded by
id from Princeton.

This maps a task into a Crucible ExperimentSpec: each distinct question becomes a
reproduction claim whose reported value is the mean of its runs and whose
tolerance is sized from the answer key itself; a mechanical positive control (the
entry runs and exits cleanly) stands in because CORE-Bench tasks carry no
separate baseline number.

Tolerance (see `tolerance_for`) is the max of three independently justified
quantities, never a magic constant:

  1. the 95% prediction interval for one new run, from the key's own replicates;
  2. the interval a rounded value stands for (0.88 means [0.875, 0.885));
  3. an `np.isclose`-style float-comparison floor.

This mirrors CORE-Bench's own grading rule. An earlier version used
`max(0.01, max-min)`, which was wrong in both directions: the 0.01 absolute floor
is larger than the entire value for small-magnitude metrics (capsule-3272782
reports an average FNMR of 0.0093) and meaninglessly tight for count-valued
answers, while `max-min` of three samples systematically understates the spread a
fourth run can show. `scripts/answer_key_variance.py` quantifies the affected
tasks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from statistics import fmean

from crucible.adjudicator.stats import prediction_interval_halfwidth
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

# np.isclose defaults — the floor below which two floats are the same number.
ISCLOSE_RTOL = 1e-5
ISCLOSE_ATOL = 1e-8


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


def _reporting_halfwidth(raw: object) -> float:
    """Half of the last printed decimal place: the interval a rounded value denotes.

    CORE-Bench answer keys mix full-precision floats with values rounded for
    publication. `0.88` stands for anything in [0.875, 0.885), so an agent that
    reports the unrounded computation must not be graded wrong for it — CORE-Bench
    makes the same allowance.

    Integral values carry no evidence of rounding and return 0.0. Python renders
    every float with a decimal point, so `0.0` and `1.0` would otherwise look like
    deliberate 1-decimal roundings and claim a 0.05 half-width — which, taken as a
    max across an answer key, let a single `0.0` replicate widen the tolerance for
    the whole question by an order of magnitude. The cost of this rule is being
    slightly tight on a metric that genuinely rounds to a whole number; the
    prediction interval dominates whenever such a key shows real scatter.
    """
    try:
        as_decimal = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0
    if as_decimal == as_decimal.to_integral_value():
        return 0.0
    exponent = as_decimal.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        return 0.0
    return 0.5 * (10.0 ** exponent)


@dataclass
class ToleranceBasis:
    """Why a tolerance is the size it is — recorded so a verdict stays auditable."""

    value: float
    basis: str
    n_runs: int
    prediction_interval: float | None
    reporting: float
    isclose_floor: float

    @property
    def estimable(self) -> bool:
        """False when a single reference run left the run-to-run spread unknown."""
        return self.prediction_interval is not None


def tolerance_for(raw_values: list[object], alpha: float = 0.05) -> ToleranceBasis:
    """Size a reproduction tolerance from an answer key's replicates.

    The three candidate widths answer different questions and the binding one is
    whichever is largest:

    - **prediction interval** — how far can a *new* run legitimately land from the
      mean, given the scatter these runs already show? Undefined for n = 1.
    - **reporting** — how much precision did the key throw away when it rounded?
    - **isclose floor** — below this, two floats are the same number.

    With one reference run and a full-precision value the result is near
    exact-match, which is the honest answer: nothing in the data licenses more.
    Callers should surface `ToleranceBasis.estimable` rather than silently
    inventing width.
    """
    numeric = [v for v in (_numeric(r) for r in raw_values) if v is not None]
    if not numeric:
        raise ValueError("tolerance_for requires at least one numeric value")

    mean = fmean(numeric)
    interval = prediction_interval_halfwidth(numeric, alpha=alpha)
    reporting = max((_reporting_halfwidth(r) for r in raw_values), default=0.0)
    floor = ISCLOSE_ATOL + ISCLOSE_RTOL * (abs(mean) + (interval or 0.0))

    widths = {"prediction_interval": interval or 0.0,
              "reporting": reporting,
              "isclose_floor": floor}
    basis = max(widths, key=lambda k: widths[k])
    return ToleranceBasis(
        value=widths[basis],
        basis=basis,
        n_runs=len(numeric),
        prediction_interval=interval,
        reporting=reporting,
        isclose_floor=floor,
    )


def unscorable_questions(task: CoreBenchTask) -> dict[str, list[object]]:
    """Questions in the answer key whose values are not numeric.

    `to_spec` can only build claims over numbers, so these are dropped. They used
    to vanish silently, which let a task be reduced to the mechanical placeholder
    claim while appearing to have been checked. Callers should report them.
    """
    grouped: dict[str, list[object]] = {}
    for run in task.results:
        for question, value in run.items():
            grouped.setdefault(question, []).append(value)
    return {q: vals for q, vals in grouped.items()
            if all(_numeric(v) is None for v in vals)}


def to_spec(task: CoreBenchTask, repo_uri: str | None = None) -> ExperimentSpec:
    # Group the answer key by question across runs/seeds, keeping the values as
    # written: their printed precision is evidence about how much rounding the
    # key applied, and float() would discard it.
    by_question: dict[str, list[object]] = {}
    for run in task.results:
        for question, value in run.items():
            if _numeric(value) is not None:
                by_question.setdefault(question, []).append(value)

    claims: list[ClaimUnderTest] = []
    for i, (question, raw_values) in enumerate(by_question.items(), start=1):
        slug = _slug(question, i)
        numeric = [v for v in (_numeric(r) for r in raw_values) if v is not None]
        reported = fmean(numeric)
        tol = tolerance_for(raw_values)
        claims.append(ClaimUnderTest(
            claim_id=f"c{i}",
            metric=slug,
            comparison=f"{slug} ~= {reported:.6g}",
            reported_values={slug: reported},
            tolerance=Tolerance(value=tol.value),
            seeds=list(range(max(1, len(numeric)))),
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
