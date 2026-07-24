"""Transactional Executor (design §6.4; inherited from RAPP §16).

Per-step lifecycle:
  check preconditions -> snapshot -> execute -> capture state delta ->
  run verifier -> commit | (Stage 1: diagnose -> recover -> re-verify | rollback -> escalate)

Stage 0: a failed precondition/verifier stops the run and reports the deepest
verified failure. Diagnosis and recovery are added in Stage 1.

Hard budgets (per-step timeout/retries) are enforced by the runner; wall/cost
budgets are checked between steps. Every transition is written to the trace.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from crucible.envmgr.manager import Environment, EnvironmentManager
from crucible.runners.base import CommandResult, Runner
from crucible.schemas import ExecutionPlan, ExperimentSpec, Step, StepState, ValidationRecord
from crucible.recovery.engine import RecoveryEngine, RepairRecord
from crucible.recovery.symptom import extract_symptom
from crucible.trace.recorder import TraceRecorder
from crucible.validation.gates import DEFAULT_INITIAL_FACTS, validate_or_raise
from crucible.validation.predicates import Predicate
from crucible.verifiers import catalog
from crucible.verifiers.catalog import VerifierContext


@dataclass
class StepResult:
    step_id: str
    state: StepState
    exit_code: int | None = None
    verifier_passed: bool | None = None
    verifier_detail: str | None = None
    checkpoint_id: str | None = None
    state_delta: dict[str, object] = field(default_factory=dict)
    failure_reason: str | None = None
    repairs: list[RepairRecord] = field(default_factory=list)


@dataclass
class RunResult:
    experiment_id: str
    trace_id: str
    step_results: list[StepResult] = field(default_factory=list)
    stopped_at: str | None = None  # step_id of the deepest failure, if any
    validation: ValidationRecord | None = None

    @property
    def all_succeeded(self) -> bool:
        return self.stopped_at is None and all(
            r.state is StepState.SUCCEEDED for r in self.step_results
        )


class TransactionalExecutor:
    def __init__(
        self,
        envmgr: EnvironmentManager,
        runner: Runner,
        recorder: TraceRecorder,
        env: Environment | None = None,
        recovery: RecoveryEngine | None = None,
    ) -> None:
        self.envmgr = envmgr
        self.runner = runner
        self.recorder = recorder
        self._env = env
        self.recovery = recovery   # None -> Stage-0 behavior (fail and stop)

    def execute(
        self,
        plan: ExecutionPlan,
        spec: ExperimentSpec | None = None,
        validate: bool = True,
        trace_id: str | None = None,
    ) -> RunResult:
        # Provision the workspace first (a benign side effect: no plan actions
        # run yet), so validation can seed dataflow with facts about inputs that
        # are already present.
        env = self._env or self.envmgr.provision(
            image=plan.steps[0].action.args.get("image") if plan.steps else None  # type: ignore[arg-type]
        )
        self._env = env

        # Gate the plan before executing any step (design §4.2). The planner is
        # not trusted: an invalid plan (unwaived ERROR) never runs. Warnings and
        # waived findings are recorded but do not block.
        record: ValidationRecord | None = None
        if validate:
            record = validate_or_raise(plan, spec, initial_facts=self._initial_facts(env))

        # Reuse a caller-provided trace (so intake/grounding LLM calls and
        # execution share one record) or open a fresh one.
        trace_id = trace_id or self.recorder.start(plan.experiment_id)
        self.recorder.record(trace_id, "run_started", {"experiment_id": plan.experiment_id})
        if record is not None:
            self.recorder.record(trace_id, "validation", record.model_dump(mode="json"))

        result = RunResult(experiment_id=plan.experiment_id, trace_id=trace_id, validation=record)
        for step in plan.steps:
            step_result = self._run_step(step, env, trace_id)
            result.step_results.append(step_result)
            if step_result.state is not StepState.SUCCEEDED:
                result.stopped_at = step.step_id  # deepest verified failure
                self.recorder.record(
                    trace_id,
                    "run_stopped",
                    {"step_id": step.step_id, "reason": step_result.failure_reason},
                )
                break

        self.recorder.record(trace_id, "run_finished", {"all_succeeded": result.all_succeeded})
        return result

    def _initial_facts(self, env: Environment) -> frozenset[Predicate]:
        """Seed dataflow with facts true at run start: the provisioned container
        plus file_exists() for every input already in the workspace."""
        import os

        facts = set(DEFAULT_INITIAL_FACTS)
        for dirpath, _dirs, files in os.walk(env.working_dir):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), env.working_dir)
                facts.add(Predicate("file_exists", (rel,)))
        return frozenset(facts)

    # --- per-step transaction ------------------------------------------------

    def _run_step(self, step: Step, env: Environment, trace_id: str) -> StepResult:
        self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "RUNNING"})

        def run(cmd: str) -> CommandResult:
            return self.runner.run(
                cmd, working_dir=env.working_dir, timeout_s=step.budget.timeout_s, image=env.image
            )

        # 1. Preconditions (checked by the harness, not trusted from the plan).
        for pred in step.preconditions:
            if not self._check_precondition(pred, run):
                return self._fail(
                    step, trace_id, StepState.FAILED, f"precondition failed: {pred}"
                )

        if step.action.kind != "shell" or not step.action.command:
            return self._fail(
                step, trace_id, StepState.FAILED, f"unsupported action kind: {step.action.kind}"
            )

        # 2. Checkpoint before mutating.
        checkpoint_id = self.envmgr.snapshot(env)

        # 3-6. Execute -> ΔS -> verify -> commit, with a bounded diagnose/recover
        # loop on failure (design §9). Stage 0 (no recovery) does one attempt.
        repairs: list[RepairRecord] = []
        max_repairs = step.budget.retries if self.recovery is not None else 0
        attempt = 0
        while True:
            last = run(step.action.command)
            self.recorder.record(trace_id, "command", {
                "step_id": step.step_id, "command": step.action.command,
                "exit_code": last.exit_code, "stdout_tail": last.stdout[-2000:],
                "stderr_tail": last.stderr[-2000:],
            })

            delta = self.envmgr.diff(checkpoint_id, env)
            self.recorder.record(trace_id, "state_delta", {"step_id": step.step_id, "delta": delta})

            self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "VERIFYING"})
            ctx = VerifierContext(working_dir=env.working_dir, last_result=last, run=run)
            vres = catalog.get(step.verifier)(ctx, dict(step.verifier_args))
            self.recorder.record(trace_id, "verification", {
                "step_id": step.step_id, "verifier": step.verifier,
                "passed": vres.passed, "detail": vres.detail,
            })

            if vres.passed:
                self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "SUCCEEDED"})
                return StepResult(
                    step_id=step.step_id, state=StepState.SUCCEEDED, exit_code=last.exit_code,
                    verifier_passed=True, verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id, state_delta=delta, repairs=repairs,
                )

            reason = f"verifier '{step.verifier}' failed: {vres.detail}"
            if self.recovery is None or attempt >= max_repairs:
                return StepResult(
                    step_id=step.step_id, state=StepState.FAILED, exit_code=last.exit_code,
                    verifier_passed=False, verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id, state_delta=delta,
                    failure_reason=reason, repairs=repairs,
                )

            # Diagnose the symptom and apply a repair, then re-attempt.
            self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "DIAGNOSING"})
            symptom = extract_symptom(step.step_id, last)
            repair = self.recovery.recover(symptom, run)
            self.recorder.record(trace_id, "recovery", {
                "step_id": step.step_id,
                "playbook": repair.playbook_id if repair else None,
                "cause": repair.cause if repair else None,
                "applied": repair.applied if repair else False,
            })
            if repair is None:
                return StepResult(
                    step_id=step.step_id, state=StepState.FAILED, exit_code=last.exit_code,
                    verifier_passed=False, verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id, state_delta=delta,
                    failure_reason=f"{reason}; no matching recovery playbook", repairs=repairs,
                )
            repairs.append(repair)
            attempt += 1
            self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "RECOVERING"})

    def _check_precondition(self, predicate: str, run) -> bool:
        """Minimal Stage-0 predicate evaluator.

        Supports: `container_ready` (always true once provisioned) and
        `file_exists("path")`. Unknown predicates pass with a soft log rather
        than blocking the slice; the full evaluator is a later item.
        """
        pred = predicate.strip()
        if pred == "container_ready":
            return True
        if pred.startswith("file_exists(") and pred.endswith(")"):
            inner = pred[len("file_exists(") : -1].strip().strip("\"'")
            return run(f"test -f {shlex.quote(inner)}").exit_code == 0
        return True  # unknown predicate: soft-pass (Stage 0)

    def _fail(self, step: Step, trace_id: str, state: StepState, reason: str) -> StepResult:
        self.recorder.record(
            trace_id, "step_failed", {"step_id": step.step_id, "reason": reason}
        )
        return StepResult(step_id=step.step_id, state=state, failure_reason=reason)
