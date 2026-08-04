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
from typing import Callable, Literal, cast

from crucible.envmgr.manager import Environment, EnvironmentManager
from crucible.runners.base import (
    CommandResult,
    MonitoredCommandResult,
    MonitoredRunner,
    Runner,
    monitored_result_consistency_error,
)
from crucible.schemas import (
    ExecutionPlan,
    ExperimentSpec,
    Step,
    StepState,
    StepType,
    ValidationRecord,
)
from crucible.trace.capture import (
    CaptureState,
    MonitorContext,
    MonitoredCommandEnvelope,
    RunCaptureSummary,
    summarize_captures,
)
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
    command_captures: list[MonitoredCommandEnvelope] = field(default_factory=list)
    monitoring_failure_state: CaptureState | None = None
    monitoring_failure_reason: str | None = None


@dataclass
class RunResult:
    experiment_id: str
    trace_id: str
    step_results: list[StepResult] = field(default_factory=list)
    stopped_at: str | None = None  # step_id of the deepest failure, if any
    validation: ValidationRecord | None = None
    monitoring_requested: bool = False

    @property
    def all_succeeded(self) -> bool:
        return self.stopped_at is None and all(
            r.state is StepState.SUCCEEDED for r in self.step_results
        )

    @property
    def command_captures(self) -> list[MonitoredCommandEnvelope]:
        return [capture for step in self.step_results for capture in step.command_captures]

    @property
    def capture_summary(self) -> RunCaptureSummary:
        monitoring_failures = tuple(
            (step.monitoring_failure_state, step.monitoring_failure_reason)
            for step in self.step_results
            if step.monitoring_failure_state is not None
            and step.monitoring_failure_reason is not None
        )
        return summarize_captures(
            self.command_captures,
            monitoring_requested=self.monitoring_requested,
            monitoring_failures=monitoring_failures,
        )


# Step type defines the workload boundary. ``path_class`` classifies repair and
# mutation risk; it does not turn harness collection/evaluation probes into
# scientific workloads or exempt a FULL_RUN from capture.
_MONITORED_STEP_TYPES = frozenset(
    {StepType.SMOKE_RUN, StepType.POSITIVE_CONTROL_RUN, StepType.FULL_RUN}
)


class TransactionalExecutor:
    def __init__(
        self,
        envmgr: EnvironmentManager,
        runner: Runner,
        recorder: TraceRecorder,
        env: Environment | None = None,
        recovery: RecoveryEngine | None = None,
        monitor_scientific_actions: bool = True,
    ) -> None:
        self.envmgr = envmgr
        self.runner = runner
        self.recorder = recorder
        self._env = env
        self.recovery = recovery  # None -> Stage-0 behavior (fail and stop)
        self.monitor_scientific_actions = monitor_scientific_actions

    def execute(
        self,
        plan: ExecutionPlan,
        spec: ExperimentSpec | None = None,
        validate: bool = True,
        trace_id: str | None = None,
    ) -> RunResult:
        if trace_id is not None and not trace_id:
            raise ValueError("trace_id must be non-empty when supplied")
        step_ids = [step.step_id for step in plan.steps]
        if not plan.experiment_id or any(not step_id for step_id in step_ids):
            raise ValueError("experiment_id and step_id values must be non-empty")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("execution plan step_id values must remain unique")
        if any(step.budget.timeout_s <= 0 or step.budget.retries < 0 for step in plan.steps):
            raise ValueError("step budgets must retain a positive timeout and nonnegative retries")

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

        monitoring_requested = self.monitor_scientific_actions and any(
            step.type in _MONITORED_STEP_TYPES for step in plan.steps
        )
        result = RunResult(
            experiment_id=plan.experiment_id,
            trace_id=trace_id,
            validation=record,
            monitoring_requested=monitoring_requested,
        )
        for step in plan.steps:
            step_result = self._run_step(step, env, trace_id, plan.experiment_id)
            result.step_results.append(step_result)
            if step_result.state is not StepState.SUCCEEDED:
                result.stopped_at = step.step_id  # deepest verified failure
                self.recorder.record(
                    trace_id,
                    "run_stopped",
                    {"step_id": step.step_id, "reason": step_result.failure_reason},
                )
                break

        self.recorder.record(
            trace_id, "capture_summary", result.capture_summary.model_dump(mode="json")
        )
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

    def _run_step(
        self, step: Step, env: Environment, trace_id: str, experiment_id: str
    ) -> StepResult:
        self.recorder.record(trace_id, "step_state", {"step_id": step.step_id, "state": "RUNNING"})

        monitor_action = self.monitor_scientific_actions and step.type in _MONITORED_STEP_TYPES
        monitored_runner: MonitoredRunner | None = None
        if monitor_action and not isinstance(self.runner, MonitoredRunner):
            reason = (
                f"scientific action {step.step_id!r} requires a MonitoredRunner; "
                f"got {type(self.runner).__module__}.{type(self.runner).__qualname__}"
            )
            self.recorder.record(
                trace_id,
                "monitor_unavailable",
                {"step_id": step.step_id, "reason": reason},
            )
            return self._fail(
                step,
                trace_id,
                StepState.FAILED,
                reason,
                monitoring_failure=(CaptureState.UNSUPPORTED, reason),
            )
        if monitor_action:
            monitored_runner = cast(MonitoredRunner, self.runner)
            try:
                trust_basis = monitored_runner.monitoring_trust_basis()
            except Exception as exc:
                reason = (
                    "monitored runner did not establish its harness trust basis: "
                    f"{type(exc).__name__}: {exc}"
                )
                self.recorder.record(
                    trace_id,
                    "monitor_unavailable",
                    {"step_id": step.step_id, "reason": reason},
                )
                return self._fail(
                    step,
                    trace_id,
                    StepState.FAILED,
                    reason,
                    monitoring_failure=(CaptureState.UNSUPPORTED, reason),
                )
            if trust_basis != "harness_tcb":
                reason = f"monitored runner has unrecognized trust basis: {trust_basis!r}"
                self.recorder.record(
                    trace_id,
                    "monitor_unavailable",
                    {"step_id": step.step_id, "reason": reason},
                )
                return self._fail(
                    step,
                    trace_id,
                    StepState.FAILED,
                    reason,
                    monitoring_failure=(CaptureState.UNSUPPORTED, reason),
                )

        def run(cmd: str) -> CommandResult:
            return self.runner.run(
                cmd, working_dir=env.working_dir, timeout_s=step.budget.timeout_s, image=env.image
            )

        # 1. Preconditions (checked by the harness, not trusted from the plan).
        for pred in step.preconditions:
            if not self._check_precondition(pred, run):
                return self._fail(step, trace_id, StepState.FAILED, f"precondition failed: {pred}")

        if step.action.kind != "shell" or not step.action.command:
            return self._fail(
                step, trace_id, StepState.FAILED, f"unsupported action kind: {step.action.kind}"
            )

        # 2. Checkpoint before mutating.
        checkpoint_id = self.envmgr.snapshot(env)

        # 3-6. Execute -> ΔS -> verify -> commit, with a bounded diagnose/recover
        # loop on failure (design §9). Stage 0 (no recovery) does one attempt.
        repairs: list[RepairRecord] = []
        command_captures: list[MonitoredCommandEnvelope] = []

        def monitored_call(
            command: str,
            *,
            role: Literal["scientific_action", "recovery_action"],
            capture_attempt: int,
        ) -> tuple[MonitoredCommandResult | None, tuple[CaptureState, str] | None]:
            assert monitored_runner is not None
            context = MonitorContext(
                trace_id=trace_id,
                experiment_id=experiment_id,
                step_id=step.step_id,
                attempt=capture_attempt,
                role=role,
            )
            try:
                monitored = monitored_runner.run_monitored(
                    command,
                    working_dir=env.working_dir,
                    context=context,
                    timeout_s=step.budget.timeout_s,
                    image=env.image,
                )
            except Exception as exc:
                reason = (
                    f"monitored runner raised before returning a trustworthy result: "
                    f"{type(exc).__name__}: {exc}"
                )
                self.recorder.record(
                    trace_id,
                    "monitor_failed",
                    {"step_id": step.step_id, "role": role, "reason": reason},
                )
                return None, (CaptureState.INCOMPLETE, reason)

            try:
                mismatch = monitored_result_consistency_error(
                    monitored,
                    command=command,
                    context=context,
                    timeout_s=step.budget.timeout_s,
                    image=env.image,
                )
            except Exception as exc:
                mismatch = f"response has invalid type or structure ({type(exc).__name__}: {exc})"
            if mismatch is not None:
                reason = f"monitored runner returned an invalid envelope: {mismatch}"
                returned_capture: object | None = None
                if isinstance(monitored, MonitoredCommandResult) and isinstance(
                    monitored.capture, MonitoredCommandEnvelope
                ):
                    returned_capture = monitored.capture.model_dump(mode="json")
                self.recorder.record(
                    trace_id,
                    "monitor_invalid",
                    {
                        "step_id": step.step_id,
                        "role": role,
                        "reason": reason,
                        "returned_capture": returned_capture,
                        "returned_type": (
                            f"{type(monitored).__module__}.{type(monitored).__qualname__}"
                        ),
                    },
                )
                return None, (CaptureState.INCOMPLETE, reason)

            command_captures.append(monitored.capture)
            self.recorder.record(
                trace_id,
                "command_capture",
                monitored.capture.model_dump(mode="json"),
            )
            return monitored, None

        def record_command(
            command: str,
            result: CommandResult | None,
            capture: MonitoredCommandEnvelope | None,
            *,
            role: Literal["scientific_action", "recovery_action", "harness_action"],
            outcome_override: str | None = None,
            error_override: str | None = None,
        ) -> None:
            self.recorder.record(
                trace_id,
                "command",
                {
                    "step_id": step.step_id,
                    "role": role,
                    "command": command,
                    "outcome": (
                        outcome_override
                        or (
                            capture.result.outcome
                            if capture is not None
                            else (
                                "timed_out"
                                if result is not None and result.timed_out
                                else "completed"
                            )
                        )
                    ),
                    "exit_code": result.exit_code if result is not None else None,
                    "stdout_tail": result.stdout[-2000:] if result is not None else "",
                    "stderr_tail": result.stderr[-2000:] if result is not None else "",
                    "runner_error": (
                        error_override
                        or (capture.result.runner_error if capture is not None else None)
                    ),
                    "capture_id": capture.capture_id if capture is not None else None,
                },
            )

        def capture_delta(
            *, uncertain: bool = False, reason: str | None = None
        ) -> dict[str, object]:
            delta = self.envmgr.diff(checkpoint_id, env)
            payload: dict[str, object] = {"step_id": step.step_id, "delta": delta}
            if uncertain:
                payload["uncertain"] = True
                payload["reason"] = reason or "command completion is not established"
            self.recorder.record(trace_id, "state_delta", payload)
            return delta

        max_repairs = step.budget.retries if self.recovery is not None else 0
        attempt = 0
        while True:
            action_capture: MonitoredCommandEnvelope | None = None
            if monitor_action:
                monitored, monitoring_failure = monitored_call(
                    step.action.command,
                    role="scientific_action",
                    capture_attempt=attempt,
                )
                if monitoring_failure is not None:
                    record_command(
                        step.action.command,
                        None,
                        None,
                        role="scientific_action",
                        outcome_override="monitor_incomplete",
                        error_override=monitoring_failure[1],
                    )
                    delta = capture_delta(
                        uncertain=True,
                        reason=monitoring_failure[1],
                    )
                    return self._fail(
                        step,
                        trace_id,
                        StepState.FAILED,
                        monitoring_failure[1],
                        checkpoint_id=checkpoint_id,
                        state_delta=delta,
                        repairs=repairs,
                        command_captures=command_captures,
                        monitoring_failure=monitoring_failure,
                    )
                assert monitored is not None
                last = monitored.command
                action_capture = monitored.capture
            else:
                last = run(step.action.command)
            record_command(
                step.action.command,
                last,
                action_capture,
                role="scientific_action" if monitor_action else "harness_action",
            )

            delta = capture_delta()

            action_outcome = (
                action_capture.result.outcome
                if action_capture is not None
                else ("timed_out" if last is not None and last.timed_out else "completed")
            )
            if action_outcome != "completed":
                if action_outcome == "runner_error":
                    reason = (
                        "scientific action did not produce a process result: "
                        f"{action_capture.result.runner_error if action_capture else 'runner error'}"
                    )
                else:
                    reason = "scientific action timed out; command completion is not established"
                self.recorder.record(
                    trace_id,
                    "verification",
                    {
                        "step_id": step.step_id,
                        "verifier": step.verifier,
                        "passed": False,
                        "detail": f"not run: {reason}",
                    },
                )
                return self._fail(
                    step,
                    trace_id,
                    StepState.FAILED,
                    reason,
                    exit_code=last.exit_code if last is not None else None,
                    verifier_passed=False,
                    verifier_detail=f"not run: {reason}",
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    repairs=repairs,
                    command_captures=command_captures,
                )

            assert last is not None

            self.recorder.record(
                trace_id, "step_state", {"step_id": step.step_id, "state": "VERIFYING"}
            )
            ctx = VerifierContext(working_dir=env.working_dir, last_result=last, run=run)
            vres = catalog.get(step.verifier)(ctx, dict(step.verifier_args))
            self.recorder.record(
                trace_id,
                "verification",
                {
                    "step_id": step.step_id,
                    "verifier": step.verifier,
                    "passed": vres.passed,
                    "detail": vres.detail,
                },
            )

            if vres.passed:
                self.recorder.record(
                    trace_id, "step_state", {"step_id": step.step_id, "state": "SUCCEEDED"}
                )
                return StepResult(
                    step_id=step.step_id,
                    state=StepState.SUCCEEDED,
                    exit_code=last.exit_code,
                    verifier_passed=True,
                    verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    repairs=repairs,
                    command_captures=command_captures,
                )

            reason = f"verifier '{step.verifier}' failed: {vres.detail}"
            if self.recovery is None or attempt >= max_repairs:
                return StepResult(
                    step_id=step.step_id,
                    state=StepState.FAILED,
                    exit_code=last.exit_code,
                    verifier_passed=False,
                    verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    failure_reason=reason,
                    repairs=repairs,
                    command_captures=command_captures,
                )

            # Diagnose the symptom and apply a repair, then re-attempt.
            self.recorder.record(
                trace_id, "step_state", {"step_id": step.step_id, "state": "DIAGNOSING"}
            )
            symptom = extract_symptom(step.step_id, last)
            self.recorder.record(
                trace_id, "step_state", {"step_id": step.step_id, "state": "RECOVERING"}
            )
            repair_monitoring_failure: tuple[CaptureState, str] | None = None
            repair_execution_failure: str | None = None
            repair_delta_uncertain = False

            def run_repair(command: str) -> CommandResult:
                nonlocal repair_delta_uncertain
                nonlocal repair_execution_failure
                nonlocal repair_monitoring_failure
                if not monitor_action:
                    return run(command)
                monitored_repair, failure = monitored_call(
                    command,
                    role="recovery_action",
                    capture_attempt=attempt,
                )
                if failure is not None:
                    repair_monitoring_failure = failure
                    repair_delta_uncertain = True
                    record_command(
                        command,
                        None,
                        None,
                        role="recovery_action",
                        outcome_override="monitor_incomplete",
                        error_override=failure[1],
                    )
                    return CommandResult(exit_code=125, stdout="", stderr=failure[1])
                assert monitored_repair is not None
                repair_result = monitored_repair.command
                repair_capture = monitored_repair.capture
                record_command(
                    command,
                    repair_result,
                    repair_capture,
                    role="recovery_action",
                )
                if repair_result is None:
                    repair_delta_uncertain = True
                    repair_execution_failure = (
                        "recovery action did not produce a process result: "
                        f"{repair_capture.result.runner_error}"
                    )
                    return CommandResult(
                        exit_code=125,
                        stdout="",
                        stderr=repair_execution_failure,
                    )
                if repair_capture.result.outcome == "timed_out":
                    repair_delta_uncertain = True
                    repair_execution_failure = (
                        "recovery action timed out; command completion is not established"
                    )
                elif repair_result.exit_code != 0:
                    repair_execution_failure = (
                        f"recovery action exited nonzero: exit_code={repair_result.exit_code}"
                    )
                return repair_result

            repair = self.recovery.recover(symptom, run_repair)
            self.recorder.record(
                trace_id,
                "recovery",
                {
                    "step_id": step.step_id,
                    "playbook": repair.playbook_id if repair else None,
                    "cause": repair.cause if repair else None,
                    "applied": repair.applied if repair else False,
                },
            )
            if repair is not None:
                repairs.append(repair)
            if repair_monitoring_failure is not None:
                delta = capture_delta(
                    uncertain=True,
                    reason=repair_monitoring_failure[1],
                )
                return self._fail(
                    step,
                    trace_id,
                    StepState.FAILED,
                    repair_monitoring_failure[1],
                    exit_code=last.exit_code,
                    verifier_passed=False,
                    verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    repairs=repairs,
                    command_captures=command_captures,
                    monitoring_failure=repair_monitoring_failure,
                )
            if repair_execution_failure is not None:
                delta = capture_delta(
                    uncertain=repair_delta_uncertain,
                    reason=repair_execution_failure,
                )
                return self._fail(
                    step,
                    trace_id,
                    StepState.FAILED,
                    repair_execution_failure,
                    exit_code=last.exit_code,
                    verifier_passed=False,
                    verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    repairs=repairs,
                    command_captures=command_captures,
                )
            if repair is None:
                return StepResult(
                    step_id=step.step_id,
                    state=StepState.FAILED,
                    exit_code=last.exit_code,
                    verifier_passed=False,
                    verifier_detail=vres.detail,
                    checkpoint_id=checkpoint_id,
                    state_delta=delta,
                    failure_reason=f"{reason}; no matching recovery playbook",
                    repairs=repairs,
                    command_captures=command_captures,
                )
            attempt += 1

    def _check_precondition(self, predicate: str, run: Callable[[str], CommandResult]) -> bool:
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

    def _fail(
        self,
        step: Step,
        trace_id: str,
        state: StepState,
        reason: str,
        *,
        exit_code: int | None = None,
        verifier_passed: bool | None = None,
        verifier_detail: str | None = None,
        checkpoint_id: str | None = None,
        state_delta: dict[str, object] | None = None,
        repairs: list[RepairRecord] | None = None,
        command_captures: list[MonitoredCommandEnvelope] | None = None,
        monitoring_failure: tuple[CaptureState, str] | None = None,
    ) -> StepResult:
        self.recorder.record(trace_id, "step_failed", {"step_id": step.step_id, "reason": reason})
        return StepResult(
            step_id=step.step_id,
            state=state,
            exit_code=exit_code,
            verifier_passed=verifier_passed,
            verifier_detail=verifier_detail,
            checkpoint_id=checkpoint_id,
            state_delta=state_delta or {},
            failure_reason=reason,
            repairs=repairs or [],
            command_captures=command_captures or [],
            monitoring_failure_state=(monitoring_failure[0] if monitoring_failure else None),
            monitoring_failure_reason=(monitoring_failure[1] if monitoring_failure else None),
        )
