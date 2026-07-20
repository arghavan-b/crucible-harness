"""Execution Plan and Step (design §4.2).

LLM-generated, harness-validated. The plan language is small and closed: every
step instantiates a StepType from the fixed ontology and every step carries a
verifier. Preconditions/postconditions are checked by the harness, never trusted
from the LLM.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import PathClass, RollbackKind
from .ontology import StepType


class Action(BaseModel):
    kind: str = Field(..., description="e.g. 'shell'")
    command: str | None = None
    working_dir: str | None = None
    args: dict[str, object] = Field(default_factory=dict)


class Rollback(BaseModel):
    kind: RollbackKind = RollbackKind.SNAPSHOT_RESTORE
    command: str | None = None


class StepBudget(BaseModel):
    timeout_s: int = 1800
    retries: int = 2


class Step(BaseModel):
    step_id: str
    type: StepType
    preconditions: list[str] = Field(
        default_factory=list, description="Predicate expressions checked by the harness."
    )
    action: Action
    postconditions: list[str] = Field(default_factory=list)
    verifier: str = Field(..., description="Verifier id; a step with no verifier does not execute.")
    verifier_args: dict[str, object] = Field(
        default_factory=dict, description="Parameters passed to the verifier (e.g. path, packages)."
    )
    rollback: Rollback = Field(default_factory=Rollback)
    budget: StepBudget = Field(default_factory=StepBudget)
    irreversible: bool = False
    path_class: PathClass = Field(
        default=PathClass.SCIENTIFIC,
        description="Infra vs scientific (design §8.2). Unknown defaults to scientific.",
    )


class ExecutionPlan(BaseModel):
    experiment_id: str
    ontology_version: str = "v1"
    steps: list[Step] = Field(default_factory=list)
