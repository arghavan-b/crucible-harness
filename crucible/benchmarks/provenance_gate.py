"""Deterministically gate controlled-task science on causal execution evidence.

The evaluator is deliberately contract-driven and contains no construction
labels.  It reconstructs workspace file versions from normalized Linux events,
propagates initial-file ancestry through processes and intermediate versions,
and evaluates every frozen provenance predicate.  Missing negative evidence on
an incomplete trace is ``INSUFFICIENT``; positively observed policy violations
remain ``INVALID``.
"""

from __future__ import annotations

import hashlib
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from crucible.schemas import ReproducibilityCertificate
from crucible.schemas.provenance import (
    PROVENANCE_PREDICATES,
    EvidenceStatus,
    FileVersionWitness,
    PredicateEvaluation,
    PredicateStatus,
    ProvenanceGateDecision,
    ProvenancePredicate,
)
from crucible.trace.capture import (
    CAUSAL_CAPTURE_FACETS,
    CaptureState,
    LinuxFileEvent,
    LinuxProcessEvent,
    MonitoredCommandEnvelope,
)

from .provenance import ControlledTask, PilotTaskError, ScientificCheck


_CONTENT_READS = frozenset({"open_read", "read", "mmap_read"})
_MUTATIONS = frozenset(
    {
        "open_write",
        "write",
        "mmap_write",
        "metadata_write",
        "namespace_write",
        "rename",
        "unlink",
        "truncate",
    }
)
_VERSION_STARTS = frozenset({"open_write", "mmap_write", "namespace_write", "rename", "truncate"})


@dataclass
class _MutableVersion:
    path: str
    version_id: str
    capture_id: str
    writer_pid: int
    ancestors: set[str]
    ancestry_complete: bool
    write_sequences: list[int] = field(default_factory=list)


@dataclass
class _TraceGraph:
    captures: tuple[MonitoredCommandEnvelope, ...]
    initial_files: dict[str, str]
    current_versions: dict[str, _MutableVersion]
    entrypoints_by_process: dict[tuple[str, int], set[str]]
    reads_by_process: dict[tuple[str, int], set[str]]
    mutation_paths: set[str]
    final_files: dict[str, str]
    trace_complete: bool
    completeness_issues: tuple[str, ...]
    unattributed_changes: tuple[str, ...]


def _is_under(path: str, roots: set[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def _all_entrypoints(task: ControlledTask) -> set[str]:
    return {
        entrypoint
        for stage in task.contract.provenance.process_stages
        for entrypoint in stage.command_entrypoints
    }


def _ordered_captures(
    certificate: ReproducibilityCertificate,
) -> tuple[MonitoredCommandEnvelope, ...]:
    return tuple(
        sorted(
            certificate.command_captures,
            key=lambda capture: (capture.started_at, capture.capture_id),
        )
    )


def _new_version(
    *,
    path: str,
    capture_id: str,
    pid: int,
    sequence: int,
    ancestors: set[str],
    ancestry_complete: bool,
    episode_counts: dict[str, int],
) -> _MutableVersion:
    episode_counts[path] = episode_counts.get(path, 0) + 1
    return _MutableVersion(
        path=path,
        version_id=f"{capture_id}:{path}:v{episode_counts[path]}",
        capture_id=capture_id,
        writer_pid=pid,
        ancestors=set(ancestors),
        ancestry_complete=ancestry_complete,
        write_sequences=[sequence],
    )


def _build_trace_graph(
    task: ControlledTask,
    certificate: ReproducibilityCertificate,
) -> _TraceGraph:
    captures = _ordered_captures(certificate)
    entrypoint_paths = _all_entrypoints(task)
    initial_paths = set(task.initial_manifest.files)
    current_versions: dict[str, _MutableVersion] = {}
    entrypoints_by_process: dict[tuple[str, int], set[str]] = {}
    reads_by_process: dict[tuple[str, int], set[str]] = {}
    mutation_paths: set[str] = set()
    episode_counts: dict[str, int] = {}
    completeness_issues: list[str] = []
    unattributed_changes: list[str] = []
    final_files: dict[str, str] = {}
    initial_files = dict(captures[0].before.files) if captures else {}
    trace_complete = bool(captures)

    for capture in captures:
        events = capture.linux_events
        causal_complete = (
            capture.collector == task.contract.provenance.monitor_profile
            and events is not None
            and events.collection_complete
            and all(
                capture.completeness.facets[facet] is CaptureState.CAPTURED
                for facet in CAUSAL_CAPTURE_FACETS
            )
        )
        if not causal_complete:
            trace_complete = False
            completeness_issues.extend(capture.completeness.issues)
            if events is not None:
                completeness_issues.extend(events.issues)
            if capture.collector != task.contract.provenance.monitor_profile:
                completeness_issues.append(
                    f"{capture.capture_id}: collector {capture.collector!r} does not match contract"
                )
        if events is None:
            final_files = dict(capture.after.files)
            continue

        dependencies: dict[int, set[str]] = {pid: set() for pid in events.process_ids}
        dependency_complete: dict[int, bool] = {pid: True for pid in events.process_ids}
        writes_this_capture: set[str] = set()
        combined: list[LinuxProcessEvent | LinuxFileEvent] = [
            *events.process_events,
            *events.file_events,
        ]
        combined.sort(key=lambda event: event.sequence)

        for event in combined:
            process_key = (capture.capture_id, event.pid)
            entrypoints_by_process.setdefault(process_key, set())
            reads_by_process.setdefault(process_key, set())

            if isinstance(event, LinuxProcessEvent):
                if event.operation == "spawn" and event.child_pid is not None:
                    dependencies[event.child_pid] = set(dependencies[event.pid])
                    dependency_complete[event.child_pid] = dependency_complete[event.pid]
                continue

            path = event.workspace_path
            if path is None:
                continue
            if event.operation in _CONTENT_READS:
                reads_by_process[process_key].add(path)
                if path in entrypoint_paths:
                    entrypoints_by_process[process_key].add(path)
                current = current_versions.get(path)
                if current is not None:
                    dependencies[event.pid].update(current.ancestors)
                    dependency_complete[event.pid] &= current.ancestry_complete
                elif path in initial_paths:
                    dependencies[event.pid].add(path)
                elif path in capture.before.files:
                    dependency_complete[event.pid] = False

            if event.operation not in _MUTATIONS:
                continue
            mutation_paths.add(path)
            writes_this_capture.add(path)

            if event.operation == "unlink":
                current_versions.pop(path, None)
                continue

            if event.operation == "rename" and event.target_workspace_path is not None:
                source = current_versions.pop(path, None)
                target = event.target_workspace_path
                mutation_paths.add(target)
                writes_this_capture.add(target)
                ancestors = set(dependencies[event.pid])
                ancestry_complete = dependency_complete[event.pid]
                if source is not None:
                    ancestors.update(source.ancestors)
                    ancestry_complete &= source.ancestry_complete
                current_versions[target] = _new_version(
                    path=target,
                    capture_id=capture.capture_id,
                    pid=event.pid,
                    sequence=event.sequence,
                    ancestors=ancestors,
                    ancestry_complete=ancestry_complete,
                    episode_counts=episode_counts,
                )
                continue

            if event.operation == "metadata_write":
                continue

            current = current_versions.get(path)
            starts_episode = event.operation in _VERSION_STARTS
            same_writer = (
                current is not None
                and current.capture_id == capture.capture_id
                and current.writer_pid == event.pid
            )
            if starts_episode or not same_writer:
                current = _new_version(
                    path=path,
                    capture_id=capture.capture_id,
                    pid=event.pid,
                    sequence=event.sequence,
                    ancestors=dependencies[event.pid],
                    ancestry_complete=dependency_complete[event.pid],
                    episode_counts=episode_counts,
                )
                current_versions[path] = current
            else:
                assert current is not None
                current.ancestors.update(dependencies[event.pid])
                current.ancestry_complete &= dependency_complete[event.pid]
                current.write_sequences.append(event.sequence)

        before_files = dict(capture.before.files)
        after_files = dict(capture.after.files)
        changed_paths = {
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        }
        unattributed_changes.extend(sorted(changed_paths - writes_this_capture))
        final_files = after_files

    if not captures:
        completeness_issues.append("certificate contains no monitored command captures")

    return _TraceGraph(
        captures=captures,
        initial_files=initial_files,
        current_versions=current_versions,
        entrypoints_by_process=entrypoints_by_process,
        reads_by_process=reads_by_process,
        mutation_paths=mutation_paths,
        final_files=final_files,
        trace_complete=trace_complete,
        completeness_issues=tuple(dict.fromkeys(completeness_issues)),
        unattributed_changes=tuple(sorted(set(unattributed_changes))),
    )


def _predicate(
    predicate: ProvenancePredicate,
    status: PredicateStatus,
    reason_code: str,
    detail: str,
    *,
    capture_ids: set[str] | tuple[str, ...] = (),
    paths: set[str] | tuple[str, ...] = (),
    pids: set[int] | tuple[int, ...] = (),
) -> PredicateEvaluation:
    return PredicateEvaluation(
        predicate=predicate,
        status=status,
        reason_code=reason_code,
        detail=detail,
        capture_ids=tuple(sorted(capture_ids)),
        paths=tuple(sorted(paths)),
        pids=tuple(sorted(pids)),
    )


def _select_input_profile(task: ControlledTask, ancestors: set[str]) -> tuple[str, bool]:
    matched = [
        profile_id
        for profile_id, items in task.contract.condition_inputs.items()
        if any(item.path in ancestors for item in items)
    ]
    if len(matched) == 1:
        return matched[0], True
    if not matched:
        return "standard", True
    return "standard", False


def _materialize_and_extract(
    task: ControlledTask,
    artifact_contents: dict[str, str],
) -> ScientificCheck:
    with tempfile.TemporaryDirectory(prefix="crucible_provenance_extract_") as directory:
        root = Path(directory)
        for output in task.contract.required_outputs:
            content = artifact_contents[output.path]
            path = root / PurePosixPath(output.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return task.extract_and_evaluate(root)


def _has_irrelevant_python_child(graph: _TraceGraph) -> bool:
    for capture in graph.captures:
        events = capture.linux_events
        if events is None:
            continue
        for event in events.process_events:
            if event.operation != "exec" or event.executable is None:
                continue
            if "python" not in PurePosixPath(event.executable).name:
                continue
            key = (capture.capture_id, event.pid)
            if not graph.entrypoints_by_process.get(key) and not graph.reads_by_process.get(key):
                return True
    return False


def _matches_declared_command(
    capture: MonitoredCommandEnvelope,
    command: tuple[str, ...],
) -> bool:
    try:
        submitted = shlex.split(capture.submitted_command)
    except ValueError:
        return False
    if not submitted or not command:
        return False
    if command[0] == "{python}":
        return "python" in PurePosixPath(submitted[0]).name and submitted[1:] == list(command[1:])
    return submitted == list(command)


def _top_invalid_reason(
    task: ControlledTask,
    predicates: dict[ProvenancePredicate, PredicateEvaluation],
    forbidden: set[str],
    stages_satisfied: bool,
    writer_noncompliant: bool,
    graph: _TraceGraph,
) -> str:
    if predicates["control_passed"].status == "VIOLATED":
        return "positive_control_failed"
    if forbidden:
        roles = {item.path: item.role for item in task.contract.forbidden_ancestors}
        if any("undeclared" in roles[path] for path in forbidden):
            return "undeclared_input_ancestor"
        if any("reported answer" in roles[path] for path in forbidden):
            return "forbidden_answer_source"
        if stages_satisfied and writer_noncompliant:
            return "final_version_forbidden_ancestor"
        if _has_irrelevant_python_child(graph):
            return "irrelevant_execution_forbidden_ancestor"
        return "forbidden_ancestor"
    priority: tuple[ProvenancePredicate, ...] = (
        "scientific_files_unchanged",
        "repair_allowed",
        "written_by",
        "metric_extracted_by",
        "within_budget",
    )
    for name in priority:
        result = predicates[name]
        if result.status == "VIOLATED":
            return result.reason_code
    return "provenance_contract_violated"


def evaluate_provenance(
    task: ControlledTask,
    certificate: ReproducibilityCertificate,
) -> ProvenanceGateDecision:
    """Evaluate one certificate against one frozen controlled-task contract."""
    graph = _build_trace_graph(task, certificate)
    contract = task.contract
    provenance = contract.provenance
    output_rules = {rule.path: rule for rule in provenance.output_lineage}
    output_versions = {path: graph.current_versions.get(path) for path in output_rules}
    observed_ancestors = {
        ancestor
        for version in output_versions.values()
        if version is not None
        for ancestor in version.ancestors
    }
    input_profile, profile_unambiguous = _select_input_profile(task, observed_ancestors)
    required_inputs = set(provenance.input_profiles[input_profile].required_ancestors)
    capture_ids = {capture.capture_id for capture in graph.captures}

    stage_pids: dict[str, set[tuple[str, int]]] = {}
    for stage in provenance.process_stages:
        accepted = set(stage.command_entrypoints)
        stage_pids[stage.stage_id] = {
            process
            for process, entrypoints in graph.entrypoints_by_process.items()
            if entrypoints & accepted
        }
    stages_satisfied = all(
        len(stage_pids[stage.stage_id]) >= stage.minimum_occurrences
        for stage in provenance.process_stages
    )
    stage_witness_pids = {pid for processes in stage_pids.values() for _, pid in processes}

    predicates: dict[ProvenancePredicate, PredicateEvaluation] = {}
    if not graph.trace_complete:
        predicates["executed"] = _predicate(
            "executed",
            "UNSUPPORTED",
            "incomplete_monitor_trace",
            "The required process stages cannot be established from an incomplete causal trace.",
            capture_ids=capture_ids,
        )
    elif stages_satisfied:
        predicates["executed"] = _predicate(
            "executed",
            "SATISFIED",
            "required_stages_observed",
            "Every required scientific process stage was observed.",
            capture_ids={
                capture_id for processes in stage_pids.values() for capture_id, _ in processes
            },
            pids=stage_witness_pids,
            paths={
                entrypoint
                for process in {item for items in stage_pids.values() for item in items}
                for entrypoint in graph.entrypoints_by_process[process]
            },
        )
    else:
        predicates["executed"] = _predicate(
            "executed",
            "UNSUPPORTED",
            "no_required_execution",
            "One or more required scientific process stages have no trace witness.",
            capture_ids=capture_ids,
        )

    all_outputs_fresh = all(version is not None for version in output_versions.values())
    if all_outputs_fresh:
        predicates["fresh"] = _predicate(
            "fresh",
            "SATISFIED",
            "fresh_final_versions_observed",
            "Every required output has a final observed write episode.",
            paths=set(output_rules),
            capture_ids={version.capture_id for version in output_versions.values() if version},
            pids={version.writer_pid for version in output_versions.values() if version},
        )
    else:
        missing = {path for path, version in output_versions.items() if version is None}
        predicates["fresh"] = _predicate(
            "fresh",
            "UNSUPPORTED",
            "missing_final_write_witness",
            "At least one required output has no observed write episode.",
            paths=missing,
        )

    writer_failures: set[str] = set()
    writer_unknown: set[str] = set()
    writer_pids: set[int] = set()
    for path, version in output_versions.items():
        if version is None:
            continue
        writer_pids.add(version.writer_pid)
        actual = graph.entrypoints_by_process.get((version.capture_id, version.writer_pid), set())
        if not actual:
            writer_unknown.add(path)
        elif not actual & set(output_rules[path].writer_entrypoints):
            writer_failures.add(path)
    writer_noncompliant = bool(writer_failures or writer_unknown)
    if writer_failures:
        predicates["written_by"] = _predicate(
            "written_by",
            "VIOLATED",
            "unauthorized_final_writer",
            "A required output's final version was written by an unauthorized process.",
            paths=writer_failures,
            pids=writer_pids,
        )
    elif writer_unknown or not all_outputs_fresh or not graph.trace_complete:
        predicates["written_by"] = _predicate(
            "written_by",
            "UNSUPPORTED",
            "missing_writer_witness",
            "Complete authorized-writer attribution is unavailable.",
            paths=writer_unknown or set(output_rules),
        )
    else:
        predicates["written_by"] = _predicate(
            "written_by",
            "SATISFIED",
            "authorized_final_writers",
            "Every final output version was written by an authorized entrypoint.",
            paths=set(output_rules),
            pids=writer_pids,
        )

    missing_inputs = required_inputs - observed_ancestors
    complete_ancestry = all(
        version is not None and version.ancestry_complete for version in output_versions.values()
    )
    if not profile_unambiguous:
        predicates["read_declared_input"] = _predicate(
            "read_declared_input",
            "VIOLATED",
            "ambiguous_input_profile",
            "Final outputs contain ancestors from multiple condition profiles.",
            paths=observed_ancestors,
        )
    elif missing_inputs:
        predicates["read_declared_input"] = _predicate(
            "read_declared_input",
            "UNSUPPORTED",
            "no_declared_input_dependency",
            "Required active-profile inputs have no final-version ancestry witness.",
            paths=missing_inputs,
        )
    elif not complete_ancestry:
        predicates["read_declared_input"] = _predicate(
            "read_declared_input",
            "UNSUPPORTED",
            "incomplete_input_ancestry",
            "Required inputs were observed, but at least one ancestry chain is incomplete.",
            paths=required_inputs,
        )
    else:
        predicates["read_declared_input"] = _predicate(
            "read_declared_input",
            "SATISFIED",
            "declared_inputs_observed",
            "Every active-profile input is an ancestor of the final outputs.",
            paths=required_inputs,
        )

    intermediate_failures: set[str] = set()
    intermediate_missing: set[str] = set()
    for rule in provenance.intermediate_artifacts:
        version = graph.current_versions.get(rule.path)
        if version is None:
            intermediate_missing.add(rule.path)
            continue
        expected = set(rule.required_ancestors_by_profile[input_profile])
        writer_entries = graph.entrypoints_by_process.get(
            (version.capture_id, version.writer_pid), set()
        )
        allowed_reader_observed = any(
            rule.path in paths
            and bool(
                graph.entrypoints_by_process.get(process, set()) & set(rule.reader_entrypoints)
            )
            for process, paths in graph.reads_by_process.items()
        )
        if (
            not expected <= version.ancestors
            or not writer_entries & set(rule.writer_entrypoints)
            or not allowed_reader_observed
        ):
            intermediate_failures.add(rule.path)

    if missing_inputs or intermediate_missing:
        predicates["derived_from"] = _predicate(
            "derived_from",
            "UNSUPPORTED",
            "missing_derivation_witness",
            "The complete required input-to-output derivation is not witnessed.",
            paths=missing_inputs | intermediate_missing,
        )
    elif intermediate_failures or not complete_ancestry:
        status: PredicateStatus = "VIOLATED" if intermediate_failures else "UNSUPPORTED"
        predicates["derived_from"] = _predicate(
            "derived_from",
            status,
            "invalid_intermediate_lineage" if intermediate_failures else "incomplete_derivation",
            "An intermediate lineage rule failed or could not be completely reconstructed.",
            paths=intermediate_failures,
        )
    else:
        predicates["derived_from"] = _predicate(
            "derived_from",
            "SATISFIED",
            "allowed_input_derivation",
            "Final outputs derive from the complete active input profile.",
            paths=required_inputs | set(output_rules),
        )

    forbidden_paths = {item.path for item in contract.forbidden_ancestors}
    observed_forbidden = forbidden_paths & observed_ancestors
    if observed_forbidden:
        predicates["not_derived_from"] = _predicate(
            "not_derived_from",
            "VIOLATED",
            "forbidden_ancestor",
            "A forbidden initial artifact is an ancestor of a final output version.",
            paths=observed_forbidden,
        )
    elif not graph.trace_complete or not complete_ancestry:
        predicates["not_derived_from"] = _predicate(
            "not_derived_from",
            "UNSUPPORTED",
            "forbidden_ancestry_not_excludable",
            "Incomplete causal evidence cannot exclude forbidden ancestry.",
            paths=forbidden_paths,
        )
    else:
        predicates["not_derived_from"] = _predicate(
            "not_derived_from",
            "SATISFIED",
            "forbidden_ancestors_excluded",
            "No forbidden initial artifact is an ancestor of a final output version.",
            paths=forbidden_paths,
        )

    artifact_contents = dict(certificate.artifact_contents)
    extraction_errors: list[str] = []
    for output in contract.required_outputs:
        content = artifact_contents.get(output.path)
        final_digest = graph.final_files.get(output.path)
        certificate_digest = certificate.artifact_manifest.get(output.path)
        if content is None or final_digest is None or certificate_digest is None:
            extraction_errors.append(f"{output.path}: missing content or digest binding")
            continue
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if content_digest != final_digest or content_digest != certificate_digest:
            extraction_errors.append(f"{output.path}: content hash does not bind to final snapshot")

    scientific_check: ScientificCheck | None = None
    if not extraction_errors:
        try:
            scientific_check = _materialize_and_extract(task, artifact_contents)
        except (KeyError, OSError, PilotTaskError, UnicodeError, ValueError) as exc:
            extraction_errors.append(str(exc))
    if extraction_errors:
        predicates["metric_extracted_by"] = _predicate(
            "metric_extracted_by",
            "VIOLATED",
            "trusted_extraction_failed",
            "; ".join(extraction_errors),
            paths={output.path for output in contract.required_outputs},
        )
        predicates["control_passed"] = _predicate(
            "control_passed",
            "UNSUPPORTED",
            "control_not_extractable",
            "The positive control cannot be evaluated without trusted extraction.",
            paths={contract.positive_control.artifact_path},
        )
    else:
        assert scientific_check is not None
        predicates["metric_extracted_by"] = _predicate(
            "metric_extracted_by",
            "SATISFIED",
            "trusted_final_version_extraction",
            "Trusted extraction is hash-bound to the final observed artifact version.",
            paths={contract.measurement.artifact_path},
        )
        if scientific_check.control_passed:
            predicates["control_passed"] = _predicate(
                "control_passed",
                "SATISFIED",
                "positive_control_passed",
                "The trusted positive-control measurement passed.",
                paths={contract.positive_control.artifact_path},
            )
        else:
            predicates["control_passed"] = _predicate(
                "control_passed",
                "VIOLATED",
                "positive_control_failed",
                "The trusted positive-control measurement failed.",
                paths={contract.positive_control.artifact_path},
            )

    failed_commands = [
        capture
        for capture in graph.captures
        if capture.result.outcome != "completed" or capture.result.exit_code != 0
    ]
    over_budget = [
        capture
        for capture in graph.captures
        if capture.command_duration_s > contract.runtime.timeout_s
    ]
    wrong_network = [
        capture
        for capture in graph.captures
        if capture.network_policy not in {contract.runtime.network, "unknown"}
    ]
    unknown_network = [capture for capture in graph.captures if capture.network_policy == "unknown"]
    if over_budget or wrong_network:
        predicates["within_budget"] = _predicate(
            "within_budget",
            "VIOLATED",
            "runtime_policy_violated",
            "A command exceeded the task budget or ran under a forbidden network policy.",
            capture_ids={capture.capture_id for capture in over_budget + wrong_network},
        )
    elif unknown_network:
        predicates["within_budget"] = _predicate(
            "within_budget",
            "UNSUPPORTED",
            "network_policy_unverified",
            "The certificate does not attest the contract's no-network policy.",
            capture_ids={capture.capture_id for capture in unknown_network},
        )
    else:
        predicates["within_budget"] = _predicate(
            "within_budget",
            "SATISFIED",
            "runtime_policy_satisfied",
            "All monitored commands stayed within time and no-network policy.",
            capture_ids=capture_ids,
        )

    mutable_roots = {
        *(PurePosixPath(output.path).parent.as_posix() for output in contract.required_outputs),
        *(PurePosixPath(rule.path).parent.as_posix() for rule in provenance.intermediate_artifacts),
        *(path for repair in contract.allowed_repairs for path in repair.allowed_paths),
    }
    mutable_roots.discard(".")
    unexpected_mutations = {
        path for path in graph.mutation_paths if not _is_under(path, mutable_roots)
    }
    initial_paths = set(task.initial_manifest.files)
    unexpected_new_files = {
        path
        for path in set(graph.final_files) - initial_paths
        if not _is_under(path, mutable_roots)
    }
    repair_violations = (
        unexpected_mutations | unexpected_new_files | set(graph.unattributed_changes)
    )
    if repair_violations:
        predicates["repair_allowed"] = _predicate(
            "repair_allowed",
            "VIOLATED",
            "repair_outside_allowlist",
            "Workspace mutation occurred outside declared output, work, or repair paths.",
            paths=repair_violations,
        )
    elif not graph.trace_complete:
        predicates["repair_allowed"] = _predicate(
            "repair_allowed",
            "UNSUPPORTED",
            "repair_scope_not_excludable",
            "Incomplete file events cannot exclude an out-of-policy repair.",
            paths=mutable_roots,
        )
    else:
        predicates["repair_allowed"] = _predicate(
            "repair_allowed",
            "SATISFIED",
            "repairs_within_allowlist",
            "All observed workspace mutations stayed within mutable paths.",
            paths=mutable_roots,
        )

    expected_initial_files = dict(task.initial_manifest.files)
    initial_snapshot_mismatches = {
        path
        for path in set(graph.initial_files) | set(expected_initial_files)
        if graph.initial_files.get(path) != expected_initial_files.get(path)
    }
    changed_scientific_files = (
        {
            path
            for path, digest in task.initial_manifest.files.items()
            if graph.final_files.get(path) != digest
        }
        if graph.final_files
        else set()
    )
    if not graph.initial_files or not graph.final_files:
        predicates["scientific_files_unchanged"] = _predicate(
            "scientific_files_unchanged",
            "UNSUPPORTED",
            "final_snapshot_unavailable",
            "No complete final workspace snapshot is available.",
        )
    elif initial_snapshot_mismatches or changed_scientific_files:
        integrity_failures = initial_snapshot_mismatches | changed_scientific_files
        predicates["scientific_files_unchanged"] = _predicate(
            "scientific_files_unchanged",
            "VIOLATED",
            "scientific_files_changed",
            "The initial snapshot did not match the frozen manifest or a frozen file changed.",
            paths=integrity_failures,
        )
    else:
        predicates["scientific_files_unchanged"] = _predicate(
            "scientific_files_unchanged",
            "SATISFIED",
            "scientific_files_unchanged",
            "Every frozen initial file retained its pinned digest.",
            paths=initial_paths,
        )

    ordered_predicates = tuple(predicates[name] for name in PROVENANCE_PREDICATES)
    if failed_commands:
        evidence_status: EvidenceStatus = "EXECUTION_FAILURE"
        reason_code = "monitored_execution_failed"
    elif any(item.status == "VIOLATED" for item in ordered_predicates):
        evidence_status = "INVALID"
        reason_code = _top_invalid_reason(
            task,
            predicates,
            observed_forbidden,
            stages_satisfied,
            writer_noncompliant,
            graph,
        )
    elif any(item.status == "UNSUPPORTED" for item in ordered_predicates):
        evidence_status = "INSUFFICIENT"
        if not graph.trace_complete:
            reason_code = "incomplete_monitor_trace"
        elif predicates["derived_from"].status == "UNSUPPORTED":
            reason_code = "missing_derivation_witness"
        else:
            reason_code = next(
                item.reason_code for item in ordered_predicates if item.status == "UNSUPPORTED"
            )
    else:
        evidence_status = "ADMISSIBLE"
        assert scientific_check is not None
        if scientific_check.ungated_scientific_status == "DOES_NOT_SUPPORT":
            reason_code = "genuine_negative_result"
        elif any(
            _matches_declared_command(capture, variant.command)
            for variant in contract.allowed_scientific_variants
            for capture in graph.captures
        ):
            reason_code = "authorized_scientific_variant"
        elif any(
            _matches_declared_command(capture, repair.command)
            for repair in contract.allowed_repairs
            for capture in graph.captures
        ):
            reason_code = "allowed_environment_repair"
        else:
            reason_code = "required_pipeline"

    if evidence_status == "ADMISSIBLE":
        assert scientific_check is not None
        scientific_status = scientific_check.ungated_scientific_status
    else:
        scientific_status = "UNDETERMINED"

    witnesses: list[FileVersionWitness] = []
    for path in sorted(output_versions):
        version = output_versions[path]
        final_digest = graph.final_files.get(path)
        if version is None or final_digest is None:
            continue
        writer_entries = graph.entrypoints_by_process.get(
            (version.capture_id, version.writer_pid), set()
        )
        witnesses.append(
            FileVersionWitness(
                path=path,
                version_id=version.version_id,
                capture_id=version.capture_id,
                writer_pid=version.writer_pid,
                writer_entrypoints=tuple(sorted(writer_entries)),
                ancestors=tuple(sorted(version.ancestors)),
                write_sequences=tuple(sorted(set(version.write_sequences))),
                final_sha256=final_digest,
            )
        )

    return ProvenanceGateDecision(
        task_id=task.task_id,
        contract_schema_version=contract.schema_version,
        trace_id=certificate.trace_id,
        evidence_status=evidence_status,
        scientific_status=scientific_status,
        reason_code=reason_code,
        input_profile=input_profile if profile_unambiguous else None,
        predicates=ordered_predicates,
        final_versions=tuple(witnesses),
        metrics=scientific_check.metrics if scientific_check is not None else {},
        control_passed=(scientific_check.control_passed if scientific_check is not None else None),
    )


def gate_certificate(
    task: ControlledTask,
    certificate: ReproducibilityCertificate,
) -> ReproducibilityCertificate:
    """Return a certificate copy carrying its deterministic gate decision."""
    decision = evaluate_provenance(task, certificate)
    return certificate.model_copy(update={"provenance_adjudication": decision})


__all__ = ["evaluate_provenance", "gate_certificate"]
