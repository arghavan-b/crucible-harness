"""Deterministic repo analysis (design §6.2 planner input).

Static inspection of a project directory — no LLM. The planner (template or
LLM-backed) consumes this: language, dependency manifests, entry points, a CUDA
hint, and top-level packages. Real intake would clone/pin the repo first; here we
analyze a directory already on disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_ENTRY_HINTS = ("main.py", "run.py", "inference.py", "train.py", "app.py", "predict.py")
_MANIFESTS = ("requirements.txt", "pyproject.toml", "environment.yml", "setup.py", "setup.cfg")
_CUDA_HINTS = ("torch", "tensorflow", "cupy", "jax", "cuda", "nvidia")


@dataclass
class RepoAnalysis:
    root: str
    language: str | None = None
    dependency_manifests: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    has_dockerfile: bool = False
    readme: str | None = None
    cuda_required: bool = False
    top_level_packages: list[str] = field(default_factory=list)


def _iter_files(root: str, max_files: int = 5000) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(out) >= max_files:
                return out
    return out


def _parse_requirements(path: str) -> list[str]:
    pkgs: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                name = re.split(r"[<>=!~;\s\[]", line, maxsplit=1)[0].strip()
                if name:
                    pkgs.append(name)
    except OSError:
        pass
    return pkgs


def _detect_entry_points(root: str, files: list[str]) -> list[str]:
    present = set(files)
    hits = [h for h in _ENTRY_HINTS if h in present]
    if hits:
        return hits
    # Fall back: any .py with a __main__ guard.
    mains: list[str] = []
    for rel in files:
        if rel.endswith(".py"):
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    if "__main__" in f.read():
                        mains.append(rel)
            except OSError:
                continue
    return mains


def analyze_repo(root: str) -> RepoAnalysis:
    files = _iter_files(root)
    lower = {f.lower() for f in files}

    language = "python" if any(f.endswith(".py") for f in files) else None
    manifests = [m for m in _MANIFESTS if m in {os.path.basename(f) for f in files}]

    readme = None
    for rel in files:
        if os.path.basename(rel).lower() in {"readme.md", "readme.rst", "readme.txt"}:
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as f:
                    readme = f.read()[:4000]
            except OSError:
                pass
            break

    packages: list[str] = []
    if "requirements.txt" in {os.path.basename(f) for f in files}:
        req = next(f for f in files if os.path.basename(f) == "requirements.txt")
        packages = _parse_requirements(os.path.join(root, req))

    blob = " ".join(packages).lower() + " " + (readme or "").lower()
    cuda = any(h in blob for h in _CUDA_HINTS)

    return RepoAnalysis(
        root=root,
        language=language,
        dependency_manifests=manifests,
        entry_points=_detect_entry_points(root, files),
        has_dockerfile="dockerfile" in lower or any(b == "dockerfile" for b in
                                                    (os.path.basename(f).lower() for f in files)),
        readme=readme,
        cuda_required=cuda,
        top_level_packages=packages,
    )
