"""Plan validation gates (design §4.2).

The planner is not trusted. Every plan passes these gates before execution:

  1. Ontology + verifiers + irreversibility: all step types in the ontology,
     every step carries a recognized verifier, destructive steps flagged
     irreversible and consistent with their rollback.
  2. smoke_run precedes full_run when scale_policy.smoke_first.
  3. positive_control_run precedes evaluate_claims (and exists if the spec
     declares positive controls) — "no positive control, no verdict".
  4. Static safety: no network egress outside the allowlist, no credential
     exfiltration patterns, per-step resource requests within budget.

`validate_plan` returns a list of Violations (empty = executable). Gates that
need the spec (2, 3, budget) are skipped when spec is None; structural and
safety gates always run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from crucible.schemas import ExecutionPlan, ExperimentSpec, RollbackKind, StepType
from crucible.validation.predicates import (
    KNOWN_PREDICATES,
    Predicate,
    PredicateSyntaxError,
    arity_ok,
    parse_predicate,
)
from crucible.verifiers import catalog

# Facts true the moment the harness hands a provisioned, writable container to
# the executor. Callers (e.g. the executor) extend this with file_exists() facts
# for inputs already present in the workspace.
DEFAULT_INITIAL_FACTS: frozenset[Predicate] = frozenset(
    {Predicate("container_ready"), Predicate("environment_writable")}
)


@dataclass
class Violation:
    gate: str
    message: str
    step_id: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.step_id}]" if self.step_id else ""
        return f"{self.gate}{loc}: {self.message}"


class PlanValidationError(Exception):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__("plan failed validation:\n  " + "\n  ".join(map(str, violations)))


# --- Gate 4 configuration -----------------------------------------------------

DEFAULT_NETWORK_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "huggingface.co",
        "zenodo.org",
    }
)

_URL_RE = re.compile(r"https?://([^/\s'\"]+)", re.IGNORECASE)
_RAW_NET_TOOL_RE = re.compile(r"\b(nc|netcat|ssh|scp|telnet|ftp)\b", re.IGNORECASE)
_SECRET_VAR_RE = re.compile(
    r"\$\{?[A-Za-z_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z_]*\}?", re.IGNORECASE
)
_ENV_DUMP_RE = re.compile(r"\b(env|printenv)\b\s*\|", re.IGNORECASE)
_DESTRUCTIVE_RES = [
    re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+system\s+prune\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force\b", re.IGNORECASE),
]


def _command_of(step) -> str:
    return step.action.command or ""


# --- Gates ---------------------------------------------------------------------


def _gate_ontology_and_verifiers(plan: ExecutionPlan) -> list[Violation]:
    out: list[Violation] = []
    valid_types = set(StepType)
    for step in plan.steps:
        if step.type not in valid_types:
            out.append(Violation("ontology", f"unknown step type {step.type!r}", step.step_id))
        if not step.verifier:
            out.append(Violation("verifier_present", "step has no verifier", step.step_id))
        elif not catalog.is_known(step.verifier):
            out.append(
                Violation("verifier_present", f"unrecognized verifier '{step.verifier}'", step.step_id)
            )
        if step.rollback.kind is RollbackKind.UNSUPPORTED and not step.irreversible:
            out.append(
                Violation(
                    "irreversible_flag",
                    "rollback is 'unsupported' but step is not flagged irreversible",
                    step.step_id,
                )
            )
        cmd = _command_of(step)
        if any(rx.search(cmd) for rx in _DESTRUCTIVE_RES) and not step.irreversible:
            out.append(
                Violation(
                    "irreversible_flag",
                    "destructive command must be flagged irreversible",
                    step.step_id,
                )
            )
    return out


def _gate_verifier_args(plan: ExecutionPlan) -> list[Violation]:
    out: list[Violation] = []
    for step in plan.steps:
        if not step.verifier or not catalog.is_known(step.verifier):
            continue  # presence/recognition already handled by gate 1
        if not catalog.is_implemented(step.verifier):
            out.append(
                Violation(
                    "verifier_not_implemented",
                    f"verifier '{step.verifier}' is catalogued but not implemented",
                    step.step_id,
                )
            )
            continue
        for err in catalog.validate_args(step.verifier, dict(step.verifier_args)):
            out.append(Violation("verifier_args", f"{step.verifier}: {err}", step.step_id))
    return out


def _first_index(plan: ExecutionPlan, step_type: StepType) -> int | None:
    for i, step in enumerate(plan.steps):
        if step.type is step_type:
            return i
    return None


def _gate_smoke_before_full(plan: ExecutionPlan, spec: ExperimentSpec) -> list[Violation]:
    if not spec.scale_policy.smoke_first:
        return []
    full_idx = _first_index(plan, StepType.FULL_RUN)
    if full_idx is None:
        return []
    smoke_idx = _first_index(plan, StepType.SMOKE_RUN)
    if smoke_idx is None or smoke_idx > full_idx:
        return [
            Violation(
                "smoke_before_full",
                "scale_policy.smoke_first is set but no smoke_run precedes full_run",
            )
        ]
    return []


def _gate_control_before_eval(plan: ExecutionPlan, spec: ExperimentSpec) -> list[Violation]:
    out: list[Violation] = []
    eval_idx = _first_index(plan, StepType.EVALUATE_CLAIMS)
    control_idx = _first_index(plan, StepType.POSITIVE_CONTROL_RUN)
    if spec.has_positive_control() and control_idx is None:
        out.append(
            Violation(
                "positive_control_required",
                "spec declares positive controls but plan has no positive_control_run "
                "(no positive control, no verdict)",
            )
        )
    if eval_idx is not None and (control_idx is None or control_idx > eval_idx):
        out.append(
            Violation("control_before_eval", "positive_control_run must precede evaluate_claims")
        )
    return out


def _gate_static_safety(
    plan: ExecutionPlan, spec: ExperimentSpec | None, allowlist: frozenset[str]
) -> list[Violation]:
    out: list[Violation] = []
    wall_budget_s = spec.budget.max_wall_hours * 3600 if spec else None
    for step in plan.steps:
        cmd = _command_of(step)

        hosts = {h.split("@")[-1].split(":")[0].lower() for h in _URL_RE.findall(cmd)}
        for host in hosts:
            if host not in allowlist:
                out.append(
                    Violation("network_allowlist", f"egress to non-allowlisted host '{host}'", step.step_id)
                )
        if _RAW_NET_TOOL_RE.search(cmd):
            out.append(
                Violation(
                    "network_allowlist",
                    "raw network tool (nc/ssh/scp/…) cannot be allowlist-checked",
                    step.step_id,
                )
            )

        has_egress = bool(hosts) or bool(_RAW_NET_TOOL_RE.search(cmd)) or "curl" in cmd or "wget" in cmd
        if has_egress and (_SECRET_VAR_RE.search(cmd) or _ENV_DUMP_RE.search(cmd)):
            out.append(
                Violation(
                    "credential_safety",
                    "possible credential exfiltration: secret/env referenced alongside network egress",
                    step.step_id,
                )
            )

        if wall_budget_s is not None and step.budget.timeout_s > wall_budget_s:
            out.append(
                Violation(
                    "budget",
                    f"step timeout {step.budget.timeout_s}s exceeds wall budget {int(wall_budget_s)}s",
                    step.step_id,
                )
            )
    return out


def _parsed_conditions(
    conditions: list[str], step_id: str, gate_out: list[Violation]
) -> list[Predicate]:
    """Parse a step's condition strings, recording violations for anything that
    is malformed, unknown, or of the wrong arity. Returns the well-formed,
    known predicates (usable for dataflow)."""
    parsed: list[Predicate] = []
    for text in conditions:
        try:
            pred = parse_predicate(text)
        except PredicateSyntaxError as exc:
            gate_out.append(Violation("predicate_syntax", str(exc), step_id))
            continue
        if pred.name not in KNOWN_PREDICATES:
            gate_out.append(
                Violation("unknown_predicate", f"'{pred.name}' not in the predicate vocabulary", step_id)
            )
            continue
        if not arity_ok(pred):
            gate_out.append(
                Violation(
                    "predicate_arity",
                    f"'{pred.name}' got {len(pred.args)} arg(s); expected arity "
                    f"{KNOWN_PREDICATES[pred.name]}",
                    step_id,
                )
            )
            continue
        parsed.append(pred)
    return parsed


def _gate_predicates_and_dataflow(
    plan: ExecutionPlan, initial_facts: frozenset[Predicate]
) -> list[Violation]:
    out: list[Violation] = []
    pre: dict[str, list[Predicate]] = {}
    post: dict[str, list[Predicate]] = {}
    for step in plan.steps:
        pre[step.step_id] = _parsed_conditions(step.preconditions, step.step_id, out)
        post[step.step_id] = _parsed_conditions(step.postconditions, step.step_id, out)

    produced_anywhere: set[Predicate] = set()
    for preds in post.values():
        produced_anywhere |= set(preds)

    # Dataflow: walk in order, requiring each hard precondition to be already
    # established by an initial fact or a prior step's postcondition.
    established: set[Predicate] = set(initial_facts)
    for step in plan.steps:
        for need in pre[step.step_id]:
            if need in established:
                continue
            if need in produced_anywhere:
                out.append(
                    Violation(
                        "dataflow",
                        f"precondition {need} is established only by a later step (ordering)",
                        step.step_id,
                    )
                )
            else:
                out.append(
                    Violation(
                        "dataflow",
                        f"precondition {need} is never established by any step or initial fact",
                        step.step_id,
                    )
                )
        established |= set(post[step.step_id])
    return out


def validate_plan(
    plan: ExecutionPlan,
    spec: ExperimentSpec | None = None,
    network_allowlist: frozenset[str] = DEFAULT_NETWORK_ALLOWLIST,
    initial_facts: frozenset[Predicate] = DEFAULT_INITIAL_FACTS,
) -> list[Violation]:
    """Return an empty list iff the plan is executable. Never raises."""
    violations: list[Violation] = []
    violations += _gate_ontology_and_verifiers(plan)
    violations += _gate_verifier_args(plan)
    violations += _gate_predicates_and_dataflow(plan, initial_facts)
    if spec is not None:
        violations += _gate_smoke_before_full(plan, spec)
        violations += _gate_control_before_eval(plan, spec)
    violations += _gate_static_safety(plan, spec, network_allowlist)
    return violations


def validate_or_raise(
    plan: ExecutionPlan,
    spec: ExperimentSpec | None = None,
    network_allowlist: frozenset[str] = DEFAULT_NETWORK_ALLOWLIST,
    initial_facts: frozenset[Predicate] = DEFAULT_INITIAL_FACTS,
) -> None:
    violations = validate_plan(plan, spec, network_allowlist, initial_facts)
    if violations:
        raise PlanValidationError(violations)
