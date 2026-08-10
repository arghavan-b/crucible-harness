"""Non-provenance verifier baselines, projected over an already-captured certificate.

Protocol §9 requires every system under comparison to see the *same* underlying
execution and to be reduced to one binary evidence decision before scientific
status is scored. Every system in this module is therefore a pure function of a
captured :class:`ReproducibilityCertificate`. Adding a comparator costs no extra
execution, cannot perturb the trace it is scored on, and keeps provenance as the
only difference between the baseline and the full gate.

**B3 — the filesystem-freshness baseline** — is the confirmatory comparator for
H1 and H2. §9 grants it initial and final content hashes plus file
creation/write observations, and withholds:

* read-dependency edges (which process read which input);
* forbidden ancestors;
* final-version lineage and write-episode structure;
* writer attribution / trusted process-artifact paths.

Those four signals are exactly the treatment. **Letting B3 consult any of them
invalidates H1**, so this module never imports the gate's trace graph and never
reads ``event.pid``. What each system is allowed to see is recorded on every
decision in ``signals_used`` so a reviewer can audit the isolation rather than
take it on trust.

Positive-control gating
-----------------------
:data:`REQUIRE_POSITIVE_CONTROL_DEFAULT` is ``True``: B3 also requires the task's
positive control to pass. That is the *stronger* baseline, and it is the right
default because the control is a scientific-validity signal available to any
verifier holding the task contract (B2 already gates on it), not a provenance
signal. Isolating provenance means giving the baseline everything except
provenance.

On the two pilot tasks this choice does **not** change the false-verification
rate. Their ``pilot-json-v1`` extractor already returns ``UNDETERMINED`` when a
control fails, so an ungated B3 abstains on I6 anyway; the flag changes B3's
evidence decision and abstention reason, not its decisiveness. That coupling is
a property of this extractor, not a law: an evaluation task whose extractor
still emits a scientific status under a failed control would separate the two
settings. The flag must therefore still be **frozen before confirmatory
scoring** (§13), and ``--freshness-ignores-control`` is for a pre-registered
sensitivity analysis, not post-hoc selection.
"""

from __future__ import annotations

from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from crucible.schemas import ReproducibilityCertificate
from crucible.schemas.provenance import ProvenanceGateDecision, ScientificStatus
from crucible.trace.capture import MonitoredCommandEnvelope

from .provenance import ControlledTask, PilotTaskError, extract_from_artifact_contents

#: Stable system identifiers used in protocol §9 and in every scored record.
PROVENANCE_SYSTEM_ID = "P"
FRESHNESS_SYSTEM_ID = "B3"

EvidenceDecision = Literal["ACCEPT", "DO_NOT_ACCEPT"]

#: Evidence classes B3 is permitted to read (§9).
FRESHNESS_SIGNALS: tuple[str, ...] = (
    "initial_content_hashes",
    "final_content_hashes",
    "file_write_observations",
    "monitored_command_outcome",
    "trusted_extraction",
)

#: Evidence classes withheld from B3 because they *are* the treatment (§9).
FRESHNESS_WITHHELD_SIGNALS: tuple[str, ...] = (
    "read_dependency_edges",
    "forbidden_ancestors",
    "final_version_lineage",
    "writer_attribution",
)

#: Evidence classes the full provenance gate reads.
PROVENANCE_SIGNALS: tuple[str, ...] = FRESHNESS_SIGNALS + FRESHNESS_WITHHELD_SIGNALS

#: File operations that count as an observed creation or write.
_WRITE_OBSERVATIONS = frozenset(
    {"open_write", "write", "mmap_write", "truncate", "namespace_write", "rename"}
)

#: See the module docstring. Freeze before confirmatory scoring.
REQUIRE_POSITIVE_CONTROL_DEFAULT = True


class SystemDecision(BaseModel):
    """One system's §9 projection of one execution: accept, then science.

    ``scientific_status`` is ``UNDETERMINED`` whenever ``evidence_decision`` is
    ``DO_NOT_ACCEPT``; the model enforces this so no scorer can accidentally
    count an abstention as a decisive verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1] = 1
    system_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    evidence_decision: EvidenceDecision
    scientific_status: ScientificStatus
    reason_code: str = Field(min_length=1)
    signals_used: tuple[str, ...]
    witnesses: tuple[str, ...] = ()
    metrics: dict[str, float] = Field(default_factory=dict)
    control_passed: StrictBool | None = None

    @property
    def decisive(self) -> bool:
        """A decisive scientific verdict, in the sense of protocol §3.2."""
        return self.scientific_status in {"SUPPORTS", "DOES_NOT_SUPPORT"}

    def model_post_init(self, _context: object) -> None:
        if self.evidence_decision == "DO_NOT_ACCEPT" and self.scientific_status != "UNDETERMINED":
            raise ValueError("a non-accepted execution cannot carry a decisive scientific status")
        if not self.signals_used:
            raise ValueError("a system decision must record the evidence classes it consumed")


def _ordered_captures(
    certificate: ReproducibilityCertificate,
) -> tuple[MonitoredCommandEnvelope, ...]:
    return tuple(
        sorted(
            certificate.command_captures,
            key=lambda capture: (capture.started_at, capture.capture_id),
        )
    )


def _write_observed_paths(
    captures: Sequence[MonitoredCommandEnvelope],
) -> tuple[set[str], bool]:
    """Return workspace paths with an observed creation/write, and whether events existed.

    The PID on each event is deliberately discarded: attributing a write to a
    process is writer attribution, which §9 withholds from B3.
    """
    observed: set[str] = set()
    events_available = False
    for capture in captures:
        events = capture.linux_events
        if events is None:
            continue
        events_available = True
        for event in events.file_events:
            if event.operation not in _WRITE_OBSERVATIONS:
                continue
            if event.operation == "rename":
                # A rename creates its target; the source is not a fresh write.
                if event.target_workspace_path is not None:
                    observed.add(event.target_workspace_path)
                continue
            if event.workspace_path is not None:
                observed.add(event.workspace_path)
    return observed, events_available


def project_gate_decision(decision: ProvenanceGateDecision) -> SystemDecision:
    """Project a full provenance-gate decision onto the common §9 binary decision."""
    accepted = decision.evidence_status == "ADMISSIBLE"
    unmet = next((item for item in decision.predicates if item.status != "SATISFIED"), None)
    return SystemDecision(
        system_id=PROVENANCE_SYSTEM_ID,
        task_id=decision.task_id,
        trace_id=decision.trace_id,
        evidence_decision="ACCEPT" if accepted else "DO_NOT_ACCEPT",
        scientific_status=decision.scientific_status,
        reason_code=decision.reason_code,
        signals_used=PROVENANCE_SIGNALS,
        witnesses=unmet.paths if unmet is not None else (),
        metrics=dict(decision.metrics),
        control_passed=decision.control_passed,
    )


def evaluate_filesystem_freshness(
    task: ControlledTask,
    certificate: ReproducibilityCertificate,
    *,
    require_positive_control: bool = REQUIRE_POSITIVE_CONTROL_DEFAULT,
) -> SystemDecision:
    """Evaluate B3: require a fresh expected artifact, and nothing about lineage.

    B3 accepts when the monitored commands succeeded, every required output
    exists in the final snapshot, differs from its initial content (or did not
    exist initially), carries at least one observed write, and yields a
    scorable trusted extraction whose positive control passes.

    It asks nothing about *where the bytes came from*. A copied, cached, or
    documentation-derived artifact that satisfies those conditions is accepted;
    distinguishing those is precisely what the provenance gate adds.
    """
    trace_id = certificate.trace_id

    def refuse(
        reason_code: str,
        *,
        witnesses: Sequence[str] = (),
        metrics: Mapping[str, float] | None = None,
        control_passed: bool | None = None,
    ) -> SystemDecision:
        return SystemDecision(
            system_id=FRESHNESS_SYSTEM_ID,
            task_id=task.task_id,
            trace_id=trace_id,
            evidence_decision="DO_NOT_ACCEPT",
            scientific_status="UNDETERMINED",
            reason_code=reason_code,
            signals_used=FRESHNESS_SIGNALS,
            witnesses=tuple(sorted(witnesses)),
            metrics=dict(metrics or {}),
            control_passed=control_passed,
        )

    captures = _ordered_captures(certificate)
    if not captures:
        return refuse("no_monitored_execution")

    failed = [
        capture.capture_id
        for capture in captures
        if capture.result.outcome != "completed" or capture.result.exit_code != 0
    ]
    if failed:
        return refuse("monitored_execution_failed", witnesses=failed)

    initial = dict(captures[0].before.files)
    final = dict(captures[-1].after.files)
    written, events_available = _write_observed_paths(captures)
    if not events_available:
        return refuse("write_observations_unavailable")

    required = [output.path for output in task.contract.required_outputs]

    missing = [path for path in required if path not in final]
    if missing:
        return refuse("missing_expected_artifact", witnesses=missing)

    stale = [path for path in required if path in initial and initial[path] == final[path]]
    if stale:
        return refuse("stale_expected_artifact", witnesses=stale)

    unwritten = [path for path in required if path not in written]
    if unwritten:
        return refuse("no_observed_write", witnesses=unwritten)

    contents = dict(certificate.artifact_contents)
    absent = [path for path in required if path not in contents]
    if absent:
        return refuse("artifact_contents_unavailable", witnesses=absent)

    try:
        check = extract_from_artifact_contents(task, contents)
    except (KeyError, OSError, PilotTaskError, UnicodeError, ValueError):
        return refuse("extraction_failed", witnesses=required)

    if require_positive_control and not check.control_passed:
        return refuse(
            "positive_control_failed",
            witnesses=[task.contract.positive_control.artifact_path],
            metrics=check.metrics,
            control_passed=False,
        )

    return SystemDecision(
        system_id=FRESHNESS_SYSTEM_ID,
        task_id=task.task_id,
        trace_id=trace_id,
        evidence_decision="ACCEPT",
        scientific_status=check.ungated_scientific_status,
        reason_code="fresh_expected_artifact",
        signals_used=FRESHNESS_SIGNALS,
        witnesses=tuple(sorted(required)),
        metrics=check.metrics,
        control_passed=check.control_passed,
    )


__all__ = [
    "FRESHNESS_SIGNALS",
    "FRESHNESS_SYSTEM_ID",
    "FRESHNESS_WITHHELD_SIGNALS",
    "PROVENANCE_SIGNALS",
    "PROVENANCE_SYSTEM_ID",
    "REQUIRE_POSITIVE_CONTROL_DEFAULT",
    "EvidenceDecision",
    "SystemDecision",
    "evaluate_filesystem_freshness",
    "project_gate_decision",
]
