"""Verifier engine — hard verifier catalog v1 (design §7).

A step without a verifier does not execute. Verifiers are versioned, calibrated
objects, not inline assertions. Stage 0 ships the deterministic (hard) set;
failure here is authoritative. Statistical/soft verifiers and calibration
tracking are Stage 1.

Verifiers run their checks THROUGH the runner (`ctx.run(...)`), so the same
verifier works whether the step executed in a host subprocess or a container.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any, Callable

from crucible.runners.base import CommandResult


@dataclass
class VerifierContext:
    """What a verifier is allowed to see: the last command's result, the working
    dir, and a `run` callable to execute checks inside the same environment."""

    working_dir: str
    last_result: CommandResult | None
    run: Callable[[str], CommandResult]


@dataclass
class VerifierResult:
    passed: bool
    detail: str | None = None


Verifier = Callable[[VerifierContext, dict[str, Any]], VerifierResult]


def _exit_code_zero(ctx: VerifierContext, args: dict[str, Any]) -> VerifierResult:
    if ctx.last_result is None:
        return VerifierResult(False, "no command result to check")
    ok = ctx.last_result.exit_code == 0
    return VerifierResult(ok, f"exit_code={ctx.last_result.exit_code}")


def _file_exists(ctx: VerifierContext, args: dict[str, Any]) -> VerifierResult:
    path = args.get("path")
    if not path:
        return VerifierResult(False, "file_exists requires 'path'")
    min_size = int(args.get("min_size", 0))
    res = ctx.run(f'test -f {shlex.quote(str(path))} && wc -c < {shlex.quote(str(path))}')
    if res.exit_code != 0:
        return VerifierResult(False, f"missing: {path}")
    try:
        size = int(res.stdout.strip() or "0")
    except ValueError:
        size = 0
    if size < min_size:
        return VerifierResult(False, f"{path} size {size} < min {min_size}")
    return VerifierResult(True, f"{path} exists, {size} bytes")


def _imports_resolvable(ctx: VerifierContext, args: dict[str, Any]) -> VerifierResult:
    packages = args.get("packages") or []
    if not packages:
        return VerifierResult(False, "imports_resolvable requires 'packages'")
    python = str(args.get("python", "python3"))
    failed: list[str] = []
    for pkg in packages:
        res = ctx.run(f'{python} -c {shlex.quote("import " + str(pkg))}')
        if res.exit_code != 0:
            failed.append(str(pkg))
    if failed:
        return VerifierResult(False, f"unresolvable: {', '.join(failed)}")
    return VerifierResult(True, f"all resolvable: {', '.join(map(str, packages))}")


_REGISTRY: dict[str, Verifier] = {
    "exit_code_zero": _exit_code_zero,
    "file_exists": _file_exists,
    "imports_resolvable": _imports_resolvable,
}


# --- verifier specifications (arg-schemas) ------------------------------------


@dataclass(frozen=True)
class ArgSpec:
    name: str
    required: bool
    argtype: type
    description: str = ""


@dataclass(frozen=True)
class VerifierSpec:
    verifier_id: str
    description: str
    args: tuple[ArgSpec, ...]
    implemented: bool


CATALOG: dict[str, VerifierSpec] = {
    "exit_code_zero": VerifierSpec(
        "exit_code_zero", "process exit code == 0", (), implemented=True
    ),
    "file_exists": VerifierSpec(
        "file_exists",
        "artifact exists at path, optionally at least min_size bytes",
        (
            ArgSpec("path", required=True, argtype=str, description="relative path to check"),
            ArgSpec("min_size", required=False, argtype=int, description="minimum size in bytes"),
        ),
        implemented=True,
    ),
    "imports_resolvable": VerifierSpec(
        "imports_resolvable",
        "all named packages import successfully in the active runtime",
        (
            ArgSpec("packages", required=True, argtype=list, description="package names to import"),
            ArgSpec("python", required=False, argtype=str, description="python executable"),
        ),
        implemented=True,
    ),
    # Catalogued for v1 but not yet implemented (Stage-0 slice ships the 3 above).
    "checksum_matches": VerifierSpec(
        "checksum_matches",
        "file sha256 matches an expected digest",
        (
            ArgSpec("path", required=True, argtype=str),
            ArgSpec("expected", required=True, argtype=str),
        ),
        implemented=False,
    ),
    "json_schema_valid": VerifierSpec(
        "json_schema_valid",
        "artifact parses as JSON and matches a schema",
        (
            ArgSpec("path", required=True, argtype=str),
            ArgSpec("schema", required=False, argtype=dict),
        ),
        implemented=False,
    ),
    "process_running": VerifierSpec(
        "process_running",
        "a named process is running",
        (ArgSpec("name", required=True, argtype=str),),
        implemented=False,
    ),
}

# Back-compat: {id: description} view of the catalog.
HARD_VERIFIERS: dict[str, str] = {vid: spec.description for vid, spec in CATALOG.items()}


def is_known(verifier_id: str) -> bool:
    return verifier_id in CATALOG


def is_implemented(verifier_id: str) -> bool:
    spec = CATALOG.get(verifier_id)
    return bool(spec and spec.implemented)


def validate_args(verifier_id: str, args: dict[str, Any]) -> list[str]:
    """Static check of verifier_args against the verifier's schema. Returns a
    list of human-readable errors (empty = valid). Unknown verifier -> one error."""
    spec = CATALOG.get(verifier_id)
    if spec is None:
        return [f"unknown verifier '{verifier_id}'"]
    errors: list[str] = []
    known = {a.name for a in spec.args}
    for a in spec.args:
        if a.name not in args:
            if a.required:
                errors.append(f"missing required arg '{a.name}'")
            continue
        value = args[a.name]
        # bool is a subclass of int; reject it where an int is expected.
        if a.argtype is int and isinstance(value, bool):
            errors.append(f"arg '{a.name}' must be int, got bool")
        elif not isinstance(value, a.argtype):
            errors.append(
                f"arg '{a.name}' must be {a.argtype.__name__}, got {type(value).__name__}"
            )
    for key in args:
        if key not in known:
            errors.append(f"unknown arg '{key}'")
    return errors


def get(verifier_id: str) -> Verifier:
    try:
        return _REGISTRY[verifier_id]
    except KeyError:
        if verifier_id in CATALOG:
            raise KeyError(
                f"verifier '{verifier_id}' is catalogued but not implemented in the Stage-0 slice"
            ) from None
        raise KeyError(
            f"unknown verifier '{verifier_id}'; available: {sorted(_REGISTRY)}"
        ) from None
