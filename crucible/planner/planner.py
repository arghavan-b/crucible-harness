"""Planner — spec + repo analysis -> typed, validated Execution Plan (design §4.2, §6.2).

Two implementations behind one protocol:

  - TemplatePlanner: deterministic. Assembles an ontology-conformant plan whose
    pre/postconditions chain so it passes the dataflow gate. No LLM; used offline
    and as the regeneration fallback.
  - LLMPlanner: a frontier LLM emits structured JSON, which is parsed against the
    schema and run through the validation gates. On failure the violations are
    fed back and the plan is regenerated (design §4.2: "either parses and
    validates or is regenerated"). Bounded attempts, then raises.

The planner NEVER executes and is never trusted; the harness validates every
plan it returns.
"""

from __future__ import annotations

from typing import Protocol

from crucible.planner.analysis import RepoAnalysis
from crucible.schemas import Action, ExecutionPlan, ExperimentSpec, Step, StepType
from crucible.validation import ValidationRecord, validate


class PlannerError(Exception):
    def __init__(self, message: str, record: ValidationRecord | None = None) -> None:
        self.record = record
        super().__init__(message)


class Planner(Protocol):
    def plan(self, spec: ExperimentSpec, analysis: RepoAnalysis) -> ExecutionPlan: ...


class LLMClient(Protocol):
    def complete_json(self, prompt: str) -> dict: ...


# --- deterministic template planner -------------------------------------------

_OUTPUT = "outputs/result.json"


def _q(path: str) -> str:
    return f'file_exists("{path}")'


class TemplatePlanner:
    """Assemble a standard reproduction plan from repo analysis.

    When `bindings` are supplied (from claim->repo grounding), the primary
    binding's concrete commands are used for the full run and the positive
    control instead of a generic `python <entry>`.
    """

    def plan(
        self, spec: ExperimentSpec, analysis: RepoAnalysis, bindings=None
    ) -> ExecutionPlan:
        binding = bindings[0] if bindings else None
        entry = (binding.entry_point if binding and binding.entry_point else None) or (
            analysis.entry_points[0] if analysis.entry_points else None
        )
        if entry is None:
            raise PlannerError("no entry point detected; cannot plan a run")
        run_cmd = (binding.run_command if binding and binding.run_command else None) or f"python {entry}"
        control_cmd = (
            binding.baseline_command if binding and binding.baseline_command else None
        ) or f"python {entry}"
        manifest = analysis.dependency_manifests[0] if analysis.dependency_manifests else None
        has_deps = bool(manifest and analysis.top_level_packages)

        steps: list[Step] = []

        acquire_post = [_q(entry)] + ([_q(manifest)] if manifest else [])
        steps.append(Step(
            step_id="acquire_source",
            type=StepType.ACQUIRE_SOURCE,
            action=Action(kind="shell", command=f"git clone {spec.source.repo_uri} . || true"),
            postconditions=acquire_post,
            verifier="file_exists",
            verifier_args={"path": entry},
        ))

        run_pre = [_q(entry)]
        if has_deps:
            steps.append(Step(
                step_id="provision_dependencies",
                type=StepType.PROVISION_DEPENDENCIES,
                preconditions=[_q(manifest)] if manifest else [],
                action=Action(kind="shell", command=f"pip install -r {manifest}"),
                postconditions=["dependencies_available"],
                verifier="imports_resolvable",
                verifier_args={"packages": analysis.top_level_packages},
            ))
            run_pre.append("dependencies_available")

        steps.append(Step(
            step_id="smoke_run",
            type=StepType.SMOKE_RUN,
            preconditions=list(run_pre),
            action=Action(kind="shell", command=f"{run_cmd} --smoke || {run_cmd}"),
            postconditions=[],
            verifier="exit_code_zero",
        ))
        steps.append(Step(
            step_id="positive_control_run",
            type=StepType.POSITIVE_CONTROL_RUN,
            preconditions=list(run_pre),
            action=Action(kind="shell", command=control_cmd),
            postconditions=[],
            verifier="exit_code_zero",
        ))
        steps.append(Step(
            step_id="full_run",
            type=StepType.FULL_RUN,
            preconditions=list(run_pre),
            action=Action(kind="shell", command=run_cmd),
            postconditions=[_q(_OUTPUT)],
            verifier="exit_code_zero",
        ))
        steps.append(Step(
            step_id="collect_artifacts",
            type=StepType.COLLECT_ARTIFACTS,
            preconditions=[_q(_OUTPUT)],
            action=Action(kind="shell", command=f"test -f {_OUTPUT}"),
            postconditions=[],
            verifier="file_exists",
            verifier_args={"path": _OUTPUT},
        ))
        steps.append(Step(
            step_id="evaluate_claims",
            type=StepType.EVALUATE_CLAIMS,
            preconditions=[_q(_OUTPUT)],
            action=Action(kind="shell", command=f"cat {_OUTPUT}"),
            postconditions=[],
            verifier="file_exists",
            verifier_args={"path": _OUTPUT},
        ))
        return ExecutionPlan(experiment_id=spec.experiment_id, steps=steps)


# --- LLM planner with validate/regenerate loop --------------------------------


def build_prompt(spec: ExperimentSpec, analysis: RepoAnalysis, feedback: str | None) -> str:
    from crucible.schemas.ontology import StepType as _ST

    ontology = ", ".join(t.value for t in _ST)
    base = (
        "You are Crucible's planner. Emit ONLY a JSON ExecutionPlan.\n"
        f"Step types (fixed ontology): {ontology}.\n"
        "Every step needs a recognized verifier and pre/postconditions from the "
        "closed predicate vocabulary. smoke_run before full_run; positive_control_run "
        "before evaluate_claims.\n\n"
        f"Spec: {spec.model_dump_json()}\n\n"
        f"Repo analysis: entry_points={analysis.entry_points}, "
        f"manifests={analysis.dependency_manifests}, packages={analysis.top_level_packages}\n"
    )
    if feedback:
        base += f"\nYour previous plan failed validation. Fix these and re-emit:\n{feedback}\n"
    return base


class LLMPlanner:
    def __init__(self, client: LLMClient, max_attempts: int = 3) -> None:
        self.client = client
        self.max_attempts = max_attempts

    def plan(self, spec: ExperimentSpec, analysis: RepoAnalysis) -> ExecutionPlan:
        feedback: str | None = None
        last: ValidationRecord | None = None
        for _ in range(self.max_attempts):
            raw = self.client.complete_json(build_prompt(spec, analysis, feedback))
            try:
                candidate = ExecutionPlan.model_validate(raw)
            except Exception as exc:  # schema parse failure counts as a failed attempt
                feedback = f"plan did not parse against the schema: {exc}"
                continue
            record = validate(candidate, spec)
            if record.passed:
                return candidate
            last = record
            feedback = "\n".join(
                f"- {f.gate} [{f.step_id}]: {f.message}" for f in record.blocking()
            )
        raise PlannerError("LLM planner could not produce a valid plan", last)
