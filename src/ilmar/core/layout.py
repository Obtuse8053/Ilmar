# SPDX-License-Identifier: Apache-2.0
"""Where Ilmar puts things on the host (ADR-0010, ADR-0011, ADR-0012).

Two disjoint roots:

* ``workspaces_root`` holds directories that may be mounted into a sandbox:
  ``<execution-id>/<attempt>/agent`` (the agent workspace) and
  ``<execution-id>/<attempt>/verify-<run>`` (each verification checkout).
* ``coordinator_root`` holds what only the coordinator touches, such as its
  own git repositories. Nothing under it is ever mounted.

All paths are computed here, purely, from validated identifiers; no task text
reaches a path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ilmar.core.naming import safe_identifier
from ilmar.domain.changes import RepositoryRef

AGENT_DIR = "agent"
VERIFY_PREFIX = "verify-"


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    workspaces_root: Path
    coordinator_root: Path

    def __post_init__(self) -> None:
        for root in (self.workspaces_root, self.coordinator_root):
            if not root.is_absolute() or root != Path(*root.parts) or ".." in root.parts:
                raise ValueError(f"layout roots must be absolute and normalised: {root}")
        if _overlaps(self.workspaces_root, self.coordinator_root):
            raise ValueError("workspaces_root and coordinator_root must not contain each other")

    @classmethod
    def under(cls, state_dir: Path) -> WorkspaceLayout:
        """The default layout inside Ilmar's state directory."""
        return cls(state_dir / "workspaces", state_dir / "coordinator")

    def attempt_dir(self, execution_id: str, attempt: int) -> Path:
        safe_identifier(execution_id, "execution id")
        if attempt < 1:
            raise ValueError("attempt starts at 1")
        return self.workspaces_root / execution_id / str(attempt)

    def agent_workspace(self, execution_id: str, attempt: int) -> Path:
        return self.attempt_dir(execution_id, attempt) / AGENT_DIR

    def verification_checkout(self, execution_id: str, attempt: int, run: int) -> Path:
        """A fresh directory per verification run, so re-verifying never reuses one."""
        if run < 1:
            raise ValueError("run starts at 1")
        return self.attempt_dir(execution_id, attempt) / f"{VERIFY_PREFIX}{run}"

    def coordinator_repository(self, repository: RepositoryRef) -> Path:
        """The coordinator-owned git repository for ``repository``; never mounted."""
        provider = safe_identifier(repository.provider, "repository provider")
        digest = hashlib.sha256(repository.slug.encode()).hexdigest()[:16]
        return self.coordinator_root / "repos" / provider / digest

    def check_mountable(self, path: Path) -> Path:
        """Return ``path`` if it may be mounted into a sandbox, else raise ``ValueError``.

        Mountable means: strictly inside ``workspaces_root``, lexically
        normalised, and not inside ``coordinator_root``.
        """
        if not path.is_absolute() or ".." in path.parts or path != Path(*path.parts):
            raise ValueError(f"mount path must be absolute and normalised: {path}")
        if not _strictly_inside(path, self.workspaces_root):
            raise ValueError(f"only paths inside {self.workspaces_root} may be mounted: {path}")
        if _overlaps(path, self.coordinator_root):
            raise ValueError(f"coordinator directories are never mounted: {path}")
        return path


def _strictly_inside(path: Path, root: Path) -> bool:
    return path != root and path.is_relative_to(root)


def _overlaps(a: Path, b: Path) -> bool:
    return a.is_relative_to(b) or b.is_relative_to(a)
