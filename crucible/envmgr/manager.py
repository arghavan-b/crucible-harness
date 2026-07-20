"""Environment Manager (design §6.3, §15).

Docker is the unit of isolation. Every mutating step commits a layer -> the
checkpoint; rollback is a layer restore. Filesystem diffs between layers are
recorded as state deltas (the original design's ΔS). Deterministic base images
per (CUDA version x Python version) matrix; uv for Python dependency operations.

Stage-0 slice ships two implementations behind one interface:
  - LocalEnvironmentManager: the workspace is a temp directory; a checkpoint is
    a copy of that directory; ΔS is a filesystem diff between copies. Runs
    anywhere, no Docker. For development and CI.
  - DockerEnvironmentManager: the real thing — workspace is a container,
    checkpoint is `docker commit` of a layer, restore re-runs from that image.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Environment:
    """A live execution context handed to the runner and verifiers."""

    env_id: str
    working_dir: str
    image: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class EnvironmentManager(Protocol):
    def provision(self, image: str | None = None) -> Environment: ...
    def snapshot(self, env: Environment) -> str: ...
    def restore(self, checkpoint_id: str) -> Environment: ...
    def diff(self, checkpoint_id: str, env: Environment) -> dict[str, object]: ...
    def teardown(self, env: Environment) -> None: ...


def _list_files(root: str) -> dict[str, int]:
    """Map of relative path -> size for every file under root."""
    out: dict[str, int] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                out[rel] = os.path.getsize(full)
            except OSError:
                out[rel] = -1
    return out


class LocalEnvironmentManager:
    """Temp-dir workspaces; checkpoints are directory copies (dev/CI)."""

    def __init__(self, base_dir: str | None = None) -> None:
        self._base = base_dir or tempfile.mkdtemp(prefix="crucible_env_")
        self._checkpoints: dict[str, str] = {}
        os.makedirs(self._base, exist_ok=True)

    def provision(self, image: str | None = None) -> Environment:
        env_id = f"env_{uuid.uuid4().hex[:8]}"
        work = os.path.join(self._base, env_id)
        os.makedirs(work, exist_ok=True)
        return Environment(env_id=env_id, working_dir=work, image=image)

    def snapshot(self, env: Environment) -> str:
        checkpoint_id = f"ckpt_{uuid.uuid4().hex[:8]}"
        dest = os.path.join(self._base, checkpoint_id)
        shutil.copytree(env.working_dir, dest)
        self._checkpoints[checkpoint_id] = dest
        return checkpoint_id

    def restore(self, checkpoint_id: str) -> Environment:
        src = self._checkpoints[checkpoint_id]
        env_id = f"env_{uuid.uuid4().hex[:8]}"
        work = os.path.join(self._base, env_id)
        shutil.copytree(src, work)
        return Environment(env_id=env_id, working_dir=work)

    def diff(self, checkpoint_id: str, env: Environment) -> dict[str, object]:
        before = _list_files(self._checkpoints[checkpoint_id])
        after = _list_files(env.working_dir)
        created = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        modified = sorted(
            p for p in set(before) & set(after) if before[p] != after[p]
        )
        return {"files_created": created, "files_removed": removed, "files_modified": modified}

    def teardown(self, env: Environment) -> None:
        shutil.rmtree(env.working_dir, ignore_errors=True)


class DockerEnvironmentManager:
    """Container workspaces; checkpoints via `docker commit` (design §6.3).

    Skeleton wired for when Docker is present. Base images come from a prebuilt
    (CUDA x Python) matrix; snapshot commits the container layer.
    """

    BASE_IMAGE_MATRIX: dict[tuple[str | None, str], str] = {
        (None, "3.12"): "crucible/base:py3.12",
        ("12.1", "3.12"): "crucible/base:cuda12.1-py3.12",
        ("12.4", "3.12"): "crucible/base:cuda12.4-py3.12",
    }

    def base_image(self, cuda_version: str | None, python_version: str) -> str:
        return self.BASE_IMAGE_MATRIX[(cuda_version, python_version)]

    def provision(self, image: str | None = None) -> Environment:
        raise NotImplementedError("Docker path: weeks 1-2 once Docker is available.")

    def snapshot(self, env: Environment) -> str:
        raise NotImplementedError("docker commit <container> -> checkpoint image ref.")

    def restore(self, checkpoint_id: str) -> Environment:
        raise NotImplementedError("docker run from committed layer.")

    def diff(self, checkpoint_id: str, env: Environment) -> dict[str, object]:
        raise NotImplementedError("docker diff / layer inspection.")

    def teardown(self, env: Environment) -> None:
        raise NotImplementedError("docker rm -f.")
