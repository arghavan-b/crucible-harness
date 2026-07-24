"""Recovery Engine (design §9.3).

Symptom -> diagnose -> match a trusted playbook -> apply the repair -> record the
outcome. The engine only *applies* the repair; the executor re-runs the step to
decide whether it actually worked (re-verification). What the LLM never does:
choose and run a repair on its own — repairs come from checked, versioned
playbooks (design §9.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from crucible.runners.base import CommandResult
from crucible.schemas import PathClass

from .diagnoser import Diagnoser, RuleDiagnoser
from .playbook import PlaybookLibrary
from .symptom import Symptom


@dataclass
class RepairRecord:
    playbook_id: str
    cause: str
    scientific: bool
    applied: bool           # did the repair command run cleanly?
    detail: str | None = None

    def __str__(self) -> str:
        return self.playbook_id


class RecoveryEngine:
    def __init__(self, library: PlaybookLibrary, diagnoser: Diagnoser | None = None) -> None:
        self.library = library
        self.diagnoser = diagnoser or RuleDiagnoser()

    def recover(
        self, symptom: Symptom, run: Callable[[str], CommandResult]
    ) -> RepairRecord | None:
        """Diagnose and apply the best matching repair. Returns a record of what
        was applied, or None if no playbook matched or required params are
        missing."""
        for diagnosis in self.diagnoser.diagnose(symptom):
            for pb in self.library.match(diagnosis.cause):
                if pb.needs_params() - set(diagnosis.params):
                    continue  # can't render (e.g. no module name extracted)
                command = pb.render(diagnosis.params)
                result = run(command)
                applied = result.exit_code == 0
                self.library.record_outcome(pb.playbook_id, applied)
                return RepairRecord(
                    playbook_id=pb.playbook_id,
                    cause=diagnosis.cause.value,
                    scientific=pb.repair.path_class is PathClass.SCIENTIFIC,
                    applied=applied,
                    detail=command,
                )
        return None
