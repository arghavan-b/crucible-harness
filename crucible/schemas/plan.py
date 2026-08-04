"""Execution Plan and Step (design §4.2).

LLM-generated, harness-validated. The plan language is small and closed: every
step instantiates a StepType from the fixed ontology and every step carries a
verifier. Preconditions/postconditions are checked by the harness, never trusted
from the LLM.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

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
    timeout_s: int = Field(default=1800, gt=0)
    retries: int = Field(default=2, ge=0)


class Step(BaseModel):
    step_id: str = Field(min_length=1)
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
    experiment_id: str = Field(min_length=1)
    ontology_version: str = "v1"
    steps: list[Step] = Field(default_factory=list)

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> ExecutionPlan:
        step_ids = [step.step_id for step in self.steps]
        duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
        if duplicates:
            raise ValueError(f"step_id values must be unique: {duplicates}")
        return self
