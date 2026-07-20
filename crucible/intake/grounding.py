"""Claim -> repo grounding (design §6.1).

Extraction says *what* number to reproduce; grounding says *how* — which script,
config, and command produce it, and where the baseline lives. This is where most
real reproductions break (the paper says 84.7 but nothing obviously emits it), so
each binding carries provenance (which repo files) and a confidence.

Two paths, mirroring the rest of intake:
  - heuristic (offline): gather static repo signals (configs, README commands,
    Makefile targets, argparse options) and match them to a claim's method /
    baseline / dataset tokens.
  - LLM: hand the claim plus those signals to the model for a concrete binding.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .extraction import ExtractedClaim
from .llm import LLMClient

_CONFIG_EXTS = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg")
_CMD_RE = re.compile(
    r"^\s*(?:\$\s*)?((?:CUDA_VISIBLE_DEVICES=\S+\s+)?"
    r"(?:python3?|bash|sh|\./|make|torchrun|accelerate|pytest|deepspeed)\b.*)$"
)
_ARG_RE = re.compile(r"""add_argument\(\s*['"](--[A-Za-z0-9_\-]+)['"]""")


class RepoLocation(BaseModel):
    file: str
    detail: str | None = None


class RepoBinding(BaseModel):
    claim_id: str
    entry_point: str | None = None
    run_command: str | None = Field(default=None, description="command that reproduces the claim (method)")
    baseline_command: str | None = Field(default=None, description="command that reproduces the baseline")
    config_files: list[str] = Field(default_factory=list)
    dataset_path: str | None = None
    sources: list[RepoLocation] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    notes: str | None = None


@dataclass
class RepoSignals:
    file_tree: list[str] = field(default_factory=list)
    config_files: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    readme_commands: list[str] = field(default_factory=list)
    makefile_targets: list[str] = field(default_factory=list)
    argparse_options: dict[str, list[str]] = field(default_factory=dict)


def _walk(root: str, limit: int = 5000) -> list[str]:
    out: list[str] = []
    for dp, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
        for name in files:
            out.append(os.path.relpath(os.path.join(dp, name), root))
            if len(out) >= limit:
                return out
    return out


def _readme_commands(root: str, files: list[str]) -> list[str]:
    cmds: list[str] = []
    for rel in files:
        if os.path.basename(rel).lower().startswith("readme"):
            try:
                text = open(os.path.join(root, rel), encoding="utf-8").read()
            except OSError:
                continue
            for line in text.splitlines():
                m = _CMD_RE.match(line)
                if m:
                    cmds.append(m.group(1).strip().strip("`"))
    # De-dup preserving order.
    seen: set[str] = set()
    return [c for c in cmds if not (c in seen or seen.add(c))]


def _makefile_targets(root: str, files: list[str]) -> list[str]:
    targets: list[str] = []
    for rel in files:
        if os.path.basename(rel) in {"Makefile", "makefile"}:
            try:
                for line in open(os.path.join(root, rel), encoding="utf-8"):
                    m = re.match(r"^([A-Za-z0-9_\-]+)\s*:(?!=)", line)
                    if m:
                        targets.append(m.group(1))
            except OSError:
                continue
    return targets


def _argparse_options(root: str, scripts: list[str]) -> dict[str, list[str]]:
    opts: dict[str, list[str]] = {}
    for rel in scripts:
        try:
            text = open(os.path.join(root, rel), encoding="utf-8").read()
        except OSError:
            continue
        found = _ARG_RE.findall(text)
        if found:
            opts[rel] = sorted(set(found))
    return opts


def gather_repo_signals(root: str) -> RepoSignals:
    files = _walk(root)
    configs = [f for f in files if f.lower().endswith(_CONFIG_EXTS)]
    # Prioritize files under configs/ or conf/.
    configs.sort(key=lambda f: (0 if re.search(r"(^|/)(configs?|conf)/", f) else 1, f))
    scripts = [f for f in files if f.endswith(".py")]
    return RepoSignals(
        file_tree=files,
        config_files=configs,
        scripts=scripts,
        readme_commands=_readme_commands(root, files),
        makefile_targets=_makefile_targets(root, files),
        argparse_options=_argparse_options(root, scripts),
    )


# --- grounding ---------------------------------------------------------------


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _match_command(commands: list[str], *needles: str | None) -> str | None:
    wants = [_norm(n) for n in needles if n and len(_norm(n)) >= 3]
    for cmd in commands:
        low = cmd.lower()
        if any(w and w in _norm(cmd) or w.replace(" ", "_") in low for w in wants):
            return cmd
    return None


def _match_configs(configs: list[str], *needles: str | None) -> list[str]:
    wants = [_norm(n).replace(" ", "") for n in needles if n and len(_norm(n)) >= 3]
    hits = [c for c in configs if any(w and w in _norm(c).replace(" ", "") for w in wants)]
    return hits


def _heuristic_binding(
    claim: ExtractedClaim, signals: RepoSignals, default_entry: str | None
) -> RepoBinding:
    run_cmd = _match_command(signals.readme_commands, claim.method, claim.metric, claim.dataset)
    base_cmd = _match_command(signals.readme_commands, claim.baseline)
    configs = _match_configs(signals.config_files, claim.method, claim.baseline, claim.dataset)
    entry = default_entry
    if run_cmd:
        toks = run_cmd.split()
        entry = next((t for t in toks if t.endswith(".py")), entry)
    sources: list[RepoLocation] = []
    if run_cmd or base_cmd:
        sources.append(RepoLocation(file="README", detail="command block"))
    for c in configs:
        sources.append(RepoLocation(file=c))
    confidence = 0.5 if run_cmd else (0.25 if configs else 0.1)
    return RepoBinding(
        claim_id=claim.claim_id,
        entry_point=entry,
        run_command=run_cmd or (f"python {entry}" if entry else None),
        baseline_command=base_cmd,
        config_files=configs,
        sources=sources,
        confidence=confidence,
        notes=None if run_cmd else "heuristic: no README command matched the method; verify manually",
    )


_GROUNDING_INSTRUCTIONS = """\
Map each claim to how the repository reproduces it. For every claim return a
binding: the entry-point script, a concrete run_command that produces the claim's
number, a baseline_command that produces the baseline number, relevant
config_files, dataset_path if identifiable, the repo files you used as sources,
and a confidence 0-1. Do not invent files or flags that are not in the signals.
Return JSON: {"bindings": [ {binding...} ]}.
"""


def _llm_ground(
    claims: list[ExtractedClaim], signals: RepoSignals, llm: LLMClient
) -> list[RepoBinding]:
    payload = {
        "claims": [c.model_dump() for c in claims],
        "config_files": signals.config_files[:80],
        "readme_commands": signals.readme_commands[:60],
        "makefile_targets": signals.makefile_targets[:40],
        "argparse_options": {k: v for k, v in list(signals.argparse_options.items())[:40]},
        "file_tree": signals.file_tree[:400],
    }
    import json

    raw = llm.complete_json(_GROUNDING_INSTRUCTIONS + "\n\nSIGNALS:\n" + json.dumps(payload))
    return [RepoBinding.model_validate(b) for b in raw.get("bindings", [])]


def ground_claims(
    claims: list[ExtractedClaim],
    root: str,
    llm: LLMClient | None = None,
    signals: RepoSignals | None = None,
    default_entry: str | None = None,
) -> list[RepoBinding]:
    signals = signals or gather_repo_signals(root)
    if not default_entry:
        default_entry = next((s for s in signals.scripts if os.path.basename(s) in
                              {"main.py", "train.py", "run.py", "inference.py"}), None)
    if llm is not None:
        return _llm_ground(claims, signals, llm)
    return [_heuristic_binding(c, signals, default_entry) for c in claims]
