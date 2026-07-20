"""Filesystem manifests — the substrate of byte-comparable reproducibility.

A manifest is `{relative_path: sha256}` for every file under a workspace. Two
runs are byte-comparable iff their produced-artifact manifests are identical;
where they differ, the differing paths ARE the documented nondeterminism sources
(design §4.4, §6.5).
"""

from __future__ import annotations

import hashlib
import os


def _iter_files(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def file_manifest(root: str, exclude: frozenset[str] = frozenset()) -> dict[str, str]:
    """Map every file under `root` (except `exclude`) to its sha256."""
    return {
        rel: sha256_file(os.path.join(root, rel))
        for rel in _iter_files(root)
        if rel not in exclude
    }


def read_paths(root: str, paths: frozenset[str]) -> dict[str, str]:
    """Read the given relative paths under `root` as text (best-effort)."""
    out: dict[str, str] = {}
    for rel in paths:
        full = os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8") as f:
                out[rel] = f.read()
        except (UnicodeDecodeError, OSError):
            continue
    return out


def read_source(root: str) -> dict[str, str]:
    """Inline the initial workspace as text (relative path -> content).

    Stage-0 slice only: production pins a git commit + dataset checksums instead
    of inlining bytes. Binary files are skipped here (text-only source repos).
    """
    source: dict[str, str] = {}
    for rel in _iter_files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                source[rel] = f.read()
        except (UnicodeDecodeError, OSError):
            continue  # non-text input: rely on pinned_inputs checksums in prod
    return source
