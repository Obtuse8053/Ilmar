# SPDX-License-Identifier: Apache-2.0
"""The authorization gate a host directory passes immediately before it is mounted.

``WorkspaceLayout.check_mountable`` is lexical only: it cannot see symlinks.
Before any container runtime mounts a directory, the coordinator calls
:func:`authorize_mount`, which adds a check on the actual filesystem. It walks
from the canonical workspaces root with ``O_NOFOLLOW`` and confirms that the
directory is the one the resource ledger recorded (ADR-0011, ADR-0012).

Remaining window: the runtime resolves the path again by name when it mounts.
Only components *above* the workspace could redirect it, and those are
coordinator-owned and never mounted (see the threat model in
:mod:`ilmar.core.safe_fs`). As a detective control, the runtime adapter
re-checks the identity of the mounted directory from inside the started
container and stops the container on a mismatch (#5).
"""

from __future__ import annotations

from pathlib import Path

from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import DirectoryIdentity, verify_directory


def authorize_mount(layout: WorkspaceLayout, path: Path, expected: DirectoryIdentity) -> Path:
    """Return ``path`` if it may be mounted now, else raise.

    Raises ``ValueError`` for a path outside the mountable area (lexical) and
    ``OwnershipConflictError`` if a symlink is involved anywhere from the root
    down, or if the directory is not the recorded one.
    """
    layout.check_mountable(path)
    verify_directory(path, root=layout.workspaces_root, expected=expected)
    return path
