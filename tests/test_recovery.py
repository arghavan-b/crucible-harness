"""Diagnosis + recovery tests (design §9)."""

from __future__ import annotations

from crucible.adjudicator import Observations, adjudicate
from crucible.envmgr.manager import LocalEnvironmentManager
from crucible.executor.executor import TransactionalExecutor
from crucible.recovery import (
    FailureCause,
    Playbook,
    PlaybookLibrary,
    RecoveryEngine,
    RuleDiagnoser,
    Symptom,
    seed_library,
)
from crucible.recovery.playbook import RepairAction
from crucible.runners.base import CommandResult, LocalSubprocessRunner
from crucible.schemas import (
    Action,
    ExecutionPlan,
    ExperimentSpec,
    Hypothesis,
    HypothesisType,
    PathClass,
    PlaybookStatus,
    PositiveControl,
    Source,
    Step,
    StepState,
    StepType,
    Tolerance,
)
from crucible.trace.recorder import SQLiteTraceRecorder


# --- diagnoser ----------------------------------------------------------------


def test_diagnose_missing_module() -> None:
    s = Symptom("s", 1, "Traceback...\nModuleNotFoundError: No module named 'torch'")
    d = RuleDiagnoser().diagnose(s)[0]
    assert d.cause is FailureCause.MISSING_DEPENDENCY
    assert d.params["module"] == "torch"


def test_diagnose_cuda_and_oom() -> None:
    assert RuleDiagnoser().diagnose(Symptom("s", 1, "CUDA error: no kernel image")) \
        [0].cause is FailureCause.CUDA_DRIVER_MISMATCH
    assert RuleDiagnoser().diagnose(Symptom("s", 1, "RuntimeError: CUDA out of memory")) \
        [0].cause is FailureCause.OUT_OF_MEMORY


def test_diagnose_unknown() -> None:
    assert RuleDiagnoser().diagnose(Symptom("s", 1, "weird nonspecific error")) \
        [0].cause is FailureCause.UNKNOWN


# --- LLM + cascading diagnosers ----------------------------------------------


def test_llm_diagnoser_parses_and_constrains() -> None:
    from crucible.intake import FakeClient
    from crucible.recovery import LLMDiagnoser

    client = FakeClient([{"diagnoses": [
        {"cause": "cuda_driver_mismatch", "confidence": 0.8, "params": {}},
        {"cause": "not_a_real_cause", "confidence": 0.9},   # dropped: not in taxonomy
    ]}])
    out = LLMDiagnoser(client).diagnose(Symptom("s", 1, "opaque error"))
    assert [d.cause for d in out] == [FailureCause.CUDA_DRIVER_MISMATCH]


def test_cascading_uses_rules_first_then_llm() -> None:
    from crucible.intake import FakeClient
    from crucible.recovery import CascadingDiagnoser, LLMDiagnoser

    # Rules resolve ModuleNotFoundError -> LLM must NOT be called.
    llm = FakeClient([])  # would raise IndexError if called
    casc = CascadingDiagnoser(RuleDiagnoser(), LLMDiagnoser(llm))
    d = casc.diagnose(Symptom("s", 1, "ModuleNotFoundError: No module named 'torch'"))
    assert d[0].cause is FailureCause.MISSING_DEPENDENCY
    assert llm.calls == []

    # Opaque error -> rules say UNKNOWN -> escalate to the LLM.
    llm2 = FakeClient([{"diagnoses": [{"cause": "code_bug", "confidence": 0.7}]}])
    casc2 = CascadingDiagnoser(RuleDiagnoser(), LLMDiagnoser(llm2))
    d2 = casc2.diagnose(Symptom("s", 1, "Segmentation fault (core dumped)"))
    assert d2[0].cause is FailureCause.CODE_BUG
    assert len(llm2.calls) == 1


# --- playbooks ----------------------------------------------------------------


def test_playbook_render_and_seed() -> None:
    pb = seed_library().match(FailureCause.MISSING_DEPENDENCY)[0]
    assert pb.render({"module": "torch"}) == "python3 -m pip install torch"


def test_promotion_lifecycle() -> None:
    lib = PlaybookLibrary([Playbook(
        playbook_id="p", cause=FailureCause.MISSING_DEPENDENCY,
        repair=RepairAction(command="true"), status=PlaybookStatus.CANDIDATE)])
    for _ in range(3):
        lib.record_outcome("p", True)
    assert lib.all()[0].status is PlaybookStatus.VALIDATED
    for _ in range(7):
        lib.record_outcome("p", True)
    assert lib.all()[0].status is PlaybookStatus.TRUSTED


# --- end-to-end recovery in the executor -------------------------------------


def _make_module_playbook(scientific: bool = False) -> PlaybookLibrary:
    # Repair "provides the dependency" by writing a local module (deterministic,
    # no network) — stands in for `pip install`.
    return PlaybookLibrary([Playbook(
        playbook_id="make_helper",
        cause=FailureCause.MISSING_DEPENDENCY,
        repair=RepairAction(
            command="printf 'VALUE=42\\n' > helper_mod.py",
            path_class=PathClass.SCIENTIFIC if scientific else PathClass.INFRASTRUCTURE,
        ),
    )])


def _import_plan() -> ExecutionPlan:
    return ExecutionPlan(experiment_id="e", steps=[Step(
        step_id="full_run", type=StepType.FULL_RUN,
        action=Action(kind="shell", command='python3 -c "import helper_mod"'),
        verifier="exit_code_zero",
    )])


def _executor(tmp_path, recovery) -> TransactionalExecutor:
    envmgr = LocalEnvironmentManager()
    return TransactionalExecutor(
        envmgr=envmgr, runner=LocalSubprocessRunner(),
        recorder=SQLiteTraceRecorder(str(tmp_path / "t.sqlite")),
        env=envmgr.provision(), recovery=recovery,
    )


def test_recovery_fixes_a_failing_step(tmp_path) -> None:
    ex = _executor(tmp_path, RecoveryEngine(_make_module_playbook()))
    run = ex.execute(_import_plan(), spec=None)
    assert run.all_succeeded
    step = run.step_results[0]
    assert step.state is StepState.SUCCEEDED
    assert [r.playbook_id for r in step.repairs] == ["make_helper"]


def test_no_recovery_still_fails(tmp_path) -> None:
    ex = _executor(tmp_path, recovery=None)  # Stage-0 behavior
    run = ex.execute(_import_plan(), spec=None)
    assert not run.all_succeeded
    assert run.step_results[0].state is StepState.FAILED
    assert not run.step_results[0].repairs


def test_scientific_path_repair_downgrades_verdict(tmp_path) -> None:
    ex = _executor(tmp_path, RecoveryEngine(_make_module_playbook(scientific=True)))
    run = ex.execute(_import_plan(), spec=None)
    assert run.all_succeeded  # execution recovered...

    spec = ExperimentSpec(
        experiment_id="e", hypothesis=Hypothesis(statement="h", type=HypothesisType.REPRODUCTION),
        source=Source(repo_uri="x", commit="c"),
        positive_controls=[PositiveControl(control_id="pc1", description="d", metric="smoke_exit_code",
                                           expected=0.0, tolerance=Tolerance(value=0.0))],
    )
    verdict = adjudicate(spec, run, "c1", Observations(control_values={"pc1": 0.0}))
    # ...but a repair touched the scientific path, so it cannot be certified.
    assert verdict.reason == "scientific_path_modified"
