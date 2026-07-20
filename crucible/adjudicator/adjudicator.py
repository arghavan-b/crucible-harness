"""Verdict Adjudicator — a decision procedure, not a model (design §8).

Stage 0 (minimal): positive controls + hard verifier integrity only.

Decision procedure per claim (§8.1):
  1. Positive control passed?  no  -> INCONCLUSIVE(control_failed). Stop.
  2. All steps SUCCEEDED with gating verifiers passed?  no -> EXECUTION_FAILURE(deepest cause). Stop.
  3. Any repair touched the scientific path?  yes -> INCONCLUSIVE(scientific_path_modified) unless human-approved.
     (Stage 0: no repairs run, so this is always no.)
  4. Compare observed metric to spec tolerance across seeds -> SUCCESS | RESULT_NEGATIVE.

INCONCLUSIVE is the default under uncertainty.
"""

from __future__ import annotations

from crucible.executor.executor import RunResult
from crucible.schemas import ExperimentSpec, Verdict


def adjudicate(spec: ExperimentSpec, run: RunResult, claim_id: str) -> Verdict:
    raise NotImplementedError("Stage 0, weeks 5-6.")
