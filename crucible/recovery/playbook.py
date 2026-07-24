"""Playbooks — parameterized, versioned repairs with an empirical record (§9.3).

A playbook maps a cause to a repair command (templated with diagnosis params),
labelled by which path it touches (infrastructure vs scientific) and by a
promotion status. The lifecycle — candidate -> validated -> trusted — is the data
flywheel: repairs that empirically work get promoted; only trusted playbooks are
applied automatically. This is the long-term moat (design §9.3, §13).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crucible.schemas import PathClass, PlaybookStatus

from .taxonomy import FailureCause

# Promotion thresholds (design §9.3): enough successful applications at a high
# enough success rate moves a playbook up a tier.
_PROMOTE_VALIDATED = (3, 0.6)   # (min successes, min success rate) candidate -> validated
_PROMOTE_TRUSTED = (10, 0.8)    # validated -> trusted


class RepairAction(BaseModel):
    kind: str = "shell"
    command: str = Field(..., description="repair command; may template {python}, {module}, ...")
    path_class: PathClass = PathClass.INFRASTRUCTURE


class PlaybookRecord(BaseModel):
    attempts: int = 0
    successes: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


class Playbook(BaseModel):
    playbook_id: str
    cause: FailureCause
    repair: RepairAction
    status: PlaybookStatus = PlaybookStatus.TRUSTED
    applicability: list[str] = Field(default_factory=list)
    record: PlaybookRecord = Field(default_factory=PlaybookRecord)
    version: int = 1

    def render(self, params: dict[str, str], python: str = "python3") -> str:
        ctx = {"python": python, **params}
        cmd = self.repair.command
        for key, val in ctx.items():
            cmd = cmd.replace("{" + key + "}", str(val))
        return cmd

    def needs_params(self) -> set[str]:
        import re
        return set(re.findall(r"\{(\w+)\}", self.repair.command)) - {"python"}


class PlaybookLibrary:
    def __init__(self, playbooks: list[Playbook] | None = None) -> None:
        self._by_id: dict[str, Playbook] = {p.playbook_id: p for p in (playbooks or [])}

    def add(self, pb: Playbook) -> None:
        self._by_id[pb.playbook_id] = pb

    def all(self) -> list[Playbook]:
        return list(self._by_id.values())

    def match(self, cause: FailureCause, trusted_only: bool = True) -> list[Playbook]:
        out = [
            p for p in self._by_id.values()
            if p.cause == cause and (not trusted_only or p.status is PlaybookStatus.TRUSTED)
        ]
        return sorted(out, key=lambda p: (p.record.success_rate, p.record.successes), reverse=True)

    def record_outcome(self, playbook_id: str, success: bool) -> None:
        pb = self._by_id.get(playbook_id)
        if pb is None:
            return
        pb.record.attempts += 1
        if success:
            pb.record.successes += 1
        self._maybe_promote(pb)

    def _maybe_promote(self, pb: Playbook) -> None:
        r = pb.record
        if pb.status is PlaybookStatus.CANDIDATE and \
                r.successes >= _PROMOTE_VALIDATED[0] and r.success_rate >= _PROMOTE_VALIDATED[1]:
            pb.status = PlaybookStatus.VALIDATED
        if pb.status is PlaybookStatus.VALIDATED and \
                r.successes >= _PROMOTE_TRUSTED[0] and r.success_rate >= _PROMOTE_TRUSTED[1]:
            pb.status = PlaybookStatus.TRUSTED


def seed_library() -> PlaybookLibrary:
    """Highest-base-rate repairs (design §14 Stage-1: ~the top playbooks). These
    are infrastructure-path only; some need network/root and are unverified here
    (like the Docker path), but they encode the real repair templates."""
    return PlaybookLibrary([
        Playbook(
            playbook_id="install_missing_dependency_v1",
            cause=FailureCause.MISSING_DEPENDENCY,
            repair=RepairAction(command="{python} -m pip install {module}"),
        ),
        Playbook(
            playbook_id="install_from_requirements_v1",
            cause=FailureCause.MISSING_DEPENDENCY,
            repair=RepairAction(command="{python} -m pip install -r requirements.txt"),
        ),
        Playbook(
            playbook_id="cuda_cpu_fallback_v1",
            cause=FailureCause.CUDA_DRIVER_MISMATCH,
            repair=RepairAction(command="export CUDA_VISIBLE_DEVICES=''"),
        ),
        Playbook(
            playbook_id="oom_cpu_fallback_v1",
            cause=FailureCause.OUT_OF_MEMORY,
            repair=RepairAction(command="export CUDA_VISIBLE_DEVICES='' ; export BATCH_SIZE=1"),
        ),
        Playbook(
            playbook_id="install_system_library_v1",
            cause=FailureCause.MISSING_SYSTEM_LIBRARY,
            repair=RepairAction(command="apt-get update && apt-get install -y {package}"),
        ),
    ])
