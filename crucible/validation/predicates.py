"""Predicate grammar for step pre/postconditions (design §4.2, RAPP §12.3).

Preconditions and postconditions are drawn from a *closed* vocabulary so the
harness can reason about them rather than treating them as free text. A predicate
is `name` or `name(arg, ...)`; args are quoted strings, bare identifiers, or
numbers. Unknown names, wrong arity, or malformed syntax are validation errors —
never silently soft-passed at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class PredicateSyntaxError(ValueError):
    pass


@dataclass(frozen=True)
class Predicate:
    name: str
    args: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.name if not self.args else f"{self.name}({', '.join(self.args)})"


# name -> arity; -1 means variadic. This is the whole vocabulary a plan may use.
KNOWN_PREDICATES: dict[str, int] = {
    "file_exists": 1,
    "artifact_exists": 1,
    "imports_resolvable": -1,        # one or more packages, or the symbol top_level_packages
    "command_available": 1,
    "runtime_language_is": 1,
    "version_satisfies": 2,
    "gpu_available": 0,
    "environment_writable": 0,
    "network_reachable": 1,
    "credential_available": 1,
    "container_ready": 0,
    "dependencies_available": 0,
    "data_available": 1,
    "configured": 0,
}

_PRED_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\((.*)\))?\s*$", re.DOTALL)
_ARG_RE = re.compile(r"""\s*("[^"]*"|'[^']*'|[^,]+)\s*""")


def _parse_args(argstr: str) -> tuple[str, ...]:
    argstr = argstr.strip()
    if not argstr:
        return ()
    args: list[str] = []
    for raw in _ARG_RE.findall(argstr):
        tok = raw.strip()
        if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
            tok = tok[1:-1]
        args.append(tok)
    return tuple(args)


def parse_predicate(text: str) -> Predicate:
    """Parse one predicate string. Raises PredicateSyntaxError on malformed input."""
    m = _PRED_RE.match(text)
    if not m:
        raise PredicateSyntaxError(f"malformed predicate: {text!r}")
    name, argstr = m.group(1), m.group(2)
    args = _parse_args(argstr) if argstr is not None else ()
    return Predicate(name, args)


def arity_ok(pred: Predicate) -> bool:
    expected = KNOWN_PREDICATES[pred.name]
    if expected < 0:
        return len(pred.args) >= 1
    return len(pred.args) == expected
