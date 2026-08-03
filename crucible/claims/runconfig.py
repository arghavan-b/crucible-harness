"""Run-config extraction — *how* the claim's number is produced.

The Procedure Compiler says which files hold the split, the metric and the
baseline. This module answers the adjacent question: what command, with what
config, actually reproduces the reported number — and what the tunable
parameters were set to.

That matters for two checks the Domain Validity Engine cannot do without it:

  - `claimed_split_is_actual` needs the split ratios/seed the code *ran with*,
    which in research repos almost always live in a config file rather than in
    the paper. A paper claiming a 70/10/20 scaffold split against a config
    holding `train_ratio: 0.5` is a finding, and it is only visible here.
  - `metric_computed_on_test` needs to know which task/entry point was invoked.

Sources, in descending trust: an executable run script (what the authors
actually ran) > a Makefile target > README command blocks (what they say to
run) > argparse defaults. Each command records where it came from, so a
reviewer can weigh them.

Static only: nothing is executed, no shell is spawned.
"""

from __future__ import annotations

import json
import os
import re

from pydantic import BaseModel, Field

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
_CONFIG_EXTS = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")
_SCRIPT_NAMES = re.compile(r"^(run|run_all|reproduce|main|train|experiment)(\.sh|\.bash)?$", re.I)
_SHELL_EXTS = (".sh", ".bash")

# A command line worth recording: an interpreter or a script invocation.
_CMD_RE = re.compile(
    r"^\s*(?:\$\s*)?((?:[A-Z_]+=\S+\s+)*"
    r"(?:python3?|bash|sh|\./|make|torchrun|accelerate|deepspeed|Rscript|julia)\b[^\n#]*)"
)
_ARG_RE = re.compile(r"--([A-Za-z0-9_-]+)(?:[=\s]+([^\s\\]+))?")
_ARGPARSE_RE = re.compile(
    r"""add_argument\(\s*['"](--[A-Za-z0-9_-]+)['"](.*?)\)""", re.S
)
_CHOICES_RE = re.compile(r"choices\s*=\s*\[([^\]]*)\]")
_DEFAULT_RE = re.compile(r"default\s*=\s*([^,)\s]+)")

# Parameters worth surfacing: the ones the validity checks reason about.
_PARAM_HINTS = re.compile(
    r"(train|val|valid|validation|test|holdout)_(ratio|frac|fraction|size|split)|"
    r"^(split|split_type|split_method|scaffold|seed|random_state|random_seed)$|"
    r"(^|_)(seed|epochs?|lr|learning_rate|batch_size|embed_dim|hidden_dim|dropout|"
    r"weight_decay|threshold|n_folds|cv|k_fold)$",
    re.I,
)
_SPLIT_KEY_RE = re.compile(
    r"(train|val|valid|validation|test|holdout)_(ratio|frac|fraction|size|split)$|"
    r"^(split|split_type|split_method)$",
    re.I,
)
_SEED_KEY_RE = re.compile(r"(^|_)(seed|random_state|random_seed)$", re.I)


class RunCommand(BaseModel):
    command: str
    source: str = Field(..., description="file the command was found in")
    kind: str = Field(..., description="run_script | makefile | readme | argparse")
    order: int = Field(0, description="position within its source; run scripts are ordered")
    args: dict[str, str | None] = Field(
        default_factory=dict, description="parsed --flags, value None when the flag is a switch"
    )

    @property
    def entry_point(self) -> str | None:
        for token in self.command.split():
            if token.endswith((".py", ".R", ".jl")):
                return token
        return None


class ConfigFile(BaseModel):
    path: str
    format: str
    sections: list[str] = Field(
        default_factory=list, description="top-level keys — often the method/task names"
    )
    params: dict[str, str] = Field(
        default_factory=dict, description="dot-path -> value, filtered to decision-relevant keys"
    )
    parse_error: str | None = None

    def split_params(self) -> dict[str, str]:
        return {k: v for k, v in self.params.items() if _SPLIT_KEY_RE.search(k.split(".")[-1])}

    def seeds(self) -> dict[str, str]:
        return {k: v for k, v in self.params.items() if _SEED_KEY_RE.search(k.split(".")[-1])}


class RunConfig(BaseModel):
    """What the repo says about running itself."""

    repo_root: str
    entry_points: list[str] = Field(default_factory=list)
    commands: list[RunCommand] = Field(default_factory=list)
    config_files: list[ConfigFile] = Field(default_factory=list)
    cli_options: dict[str, list[str]] = Field(
        default_factory=dict, description="entry point -> declared --flags"
    )
    cli_choices: dict[str, list[str]] = Field(
        default_factory=dict, description="flag -> allowed values, from argparse choices"
    )

    @property
    def reproduce_commands(self) -> list[RunCommand]:
        """The authoritative sequence: an executable run script if one exists,
        else Makefile, else README. Authors run scripts; READMEs drift."""
        for kind in ("run_script", "makefile", "readme"):
            hits = [c for c in self.commands if c.kind == kind]
            if hits:
                return sorted(hits, key=lambda c: c.order)
        return []

    def configs_referenced(self) -> list[str]:
        """Config paths named on the authoritative commands — the ones that ran.

        Scoped to `reproduce_commands` on purpose: README blocks routinely show
        placeholder invocations (`--config=config/xxx.json`) that were never
        executed, and treating those as "the config that ran" would ground the
        split against a file the authors never used.
        """
        out: list[str] = []
        for cmd in self.reproduce_commands or self.commands:
            for key, value in cmd.args.items():
                if value and ("config" in key.lower() or value.endswith(_CONFIG_EXTS)):
                    if value not in out:
                        out.append(value)
        return out

    def config_for(self, path: str) -> ConfigFile | None:
        norm = path.lstrip("./")
        return next((c for c in self.config_files if c.path.endswith(norm)), None)

    def declared_split(self) -> dict[str, str]:
        """Split parameters from the configs that were actually referenced,
        falling back to every config when none is named on a command line."""
        referenced = [self.config_for(p) for p in self.configs_referenced()]
        pool = [c for c in referenced if c] or self.config_files
        out: dict[str, str] = {}
        for cfg in pool:
            for key, value in cfg.split_params().items():
                out[f"{cfg.path}:{key}"] = value
        return out

    def declared_seeds(self) -> dict[str, str]:
        referenced = [self.config_for(p) for p in self.configs_referenced()]
        pool = [c for c in referenced if c] or self.config_files
        out: dict[str, str] = {}
        for cfg in pool:
            for key, value in cfg.seeds().items():
                out[f"{cfg.path}:{key}"] = value
        return out

    def summary(self) -> str:
        cmds = self.reproduce_commands
        if not cmds:
            return "no reproduce command found"
        src = cmds[0].source
        return f"{len(cmds)} command(s) from {src}; {len(self.config_files)} config file(s)"


# --- parsing helpers -----------------------------------------------------------


def _walk(root: str, limit: int = 20000) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(out) >= limit:
                return out
    return out


def _read(root: str, rel: str, limit: int = 400_000) -> str:
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def _parse_args(command: str) -> dict[str, str | None]:
    args: dict[str, str | None] = {}
    for name, value in _ARG_RE.findall(command):
        args[name] = value.strip("\"'") if value else None
    return args


def _commands_from(text: str, source: str, kind: str) -> list[RunCommand]:
    out: list[RunCommand] = []
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip().lstrip("`").strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _CMD_RE.match(stripped)
        if not m:
            continue
        command = m.group(1).strip().rstrip("\\").strip().strip("`")
        if len(command) < 6:
            continue
        out.append(
            RunCommand(
                command=command, source=source, kind=kind, order=i, args=_parse_args(command)
            )
        )
    return out


def _flatten(obj: object, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(value, path))
    elif isinstance(obj, list):
        if obj and all(isinstance(v, (int, float, str, bool)) for v in obj):
            flat[prefix] = json.dumps(obj)
    else:
        flat[prefix] = str(obj)
    return flat


def _parse_config(root: str, rel: str) -> ConfigFile:
    ext = os.path.splitext(rel)[1].lower()
    text = _read(root, rel)
    data: object = None
    error: str | None = None
    try:
        if ext == ".json":
            data = json.loads(text)
        elif ext in (".yaml", ".yml"):
            import yaml  # optional dependency; degrade rather than fail

            data = yaml.safe_load(text)
        elif ext == ".toml":
            import tomllib

            data = tomllib.loads(text)
        else:
            error = f"unparsed format '{ext}'"
    except Exception as exc:  # a malformed config is itself a finding
        error = f"{type(exc).__name__}: {exc}"

    sections: list[str] = []
    params: dict[str, str] = {}
    if isinstance(data, dict):
        sections = [str(k) for k in data]
        for path, value in _flatten(data).items():
            leaf = path.split(".")[-1]
            if _PARAM_HINTS.search(leaf):
                params[path] = value
    return ConfigFile(
        path=rel.replace(os.sep, "/"), format=ext.lstrip("."), sections=sections,
        params=params, parse_error=error,
    )


def _argparse_options(
    root: str, scripts: list[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    options: dict[str, list[str]] = {}
    choices: dict[str, list[str]] = {}
    for rel in scripts:
        text = _read(root, rel)
        if "add_argument" not in text:
            continue
        flags: list[str] = []
        for flag, tail in _ARGPARSE_RE.findall(text):
            flags.append(flag)
            m = _CHOICES_RE.search(tail)
            if m:
                values = [
                    v.strip().strip("\"'") for v in m.group(1).split(",") if v.strip()
                ]
                if values:
                    choices[flag] = values
        if flags:
            options[rel.replace(os.sep, "/")] = sorted(set(flags))
    return options, choices


# --- entry point ----------------------------------------------------------------


def extract_run_config(repo_root: str) -> RunConfig:
    """Find how the repo is meant to be run, and with what parameters."""
    repo_root = os.path.abspath(repo_root)
    files = _walk(repo_root)

    commands: list[RunCommand] = []
    for rel in files:
        base = os.path.basename(rel)
        norm = rel.replace(os.sep, "/")
        if _SCRIPT_NAMES.match(base) and not base.endswith(".py"):
            commands += _commands_from(_read(repo_root, rel), norm, "run_script")
        elif base.endswith(_SHELL_EXTS):
            commands += _commands_from(_read(repo_root, rel), norm, "run_script")
        elif base in ("Makefile", "makefile"):
            commands += _commands_from(_read(repo_root, rel), norm, "makefile")
        elif base.lower().startswith(("readme", "reproducing", "reproduce")):
            commands += _commands_from(_read(repo_root, rel), norm, "readme")

    config_files = [
        _parse_config(repo_root, rel)
        for rel in files
        if rel.lower().endswith(_CONFIG_EXTS)
        and not os.path.basename(rel).startswith(".")
        and os.path.basename(rel) not in ("package-lock.json", "uv.lock")
    ]
    # Configs that a command names come first — those are the ones that ran.
    named = {os.path.basename(p) for c in commands for p in c.args.values() if p}
    config_files.sort(key=lambda c: (os.path.basename(c.path) not in named, c.path))

    scripts = [f for f in files if f.endswith(".py")]
    cli_options, cli_choices = _argparse_options(repo_root, scripts)

    entry_points: list[str] = []
    for cmd in commands:
        ep = cmd.entry_point
        if ep and ep not in entry_points:
            entry_points.append(ep)
    for hint in ("main.py", "run.py", "train.py", "inference.py", "predict.py"):
        if hint in files and hint not in entry_points:
            entry_points.append(hint)

    return RunConfig(
        repo_root=repo_root,
        entry_points=entry_points,
        commands=commands,
        config_files=config_files[:40],
        cli_options=cli_options,
        cli_choices=cli_choices,
    )
