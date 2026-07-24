"""Observed failure symptoms (design §9.2).

A symptom is what we can see when a step fails: exit code and the tail of its
output. The diagnoser turns this into a ranked cause; extraction here just
captures the raw signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from crucible.runners.base import CommandResult


@dataclass
class Symptom:
    step_id: str
    exit_code: int
    text: str          # tail of stderr + stdout
    timed_out: bool = False


def extract_symptom(step_id: str, result: CommandResult, limit: int = 4000) -> Symptom:
    text = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()
    return Symptom(
        step_id=step_id,
        exit_code=result.exit_code,
        text=text[-limit:],
        timed_out=result.timed_out,
    )
