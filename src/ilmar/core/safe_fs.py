# SPDX-License-Identifier: Apache-2.0
"""Create and delete host directories without following symlinks (ADR-0012).

Workspaces are written by untrusted agents. Any directory they can write may
contain symlinks pointing anywhere on the host, and an attacker could also try
to swap a directory for a symlink between a check and an operation. These
helpers therefore:

* walk from the root one component at a time with ``O_NOFOLLOW`` directory
  file descriptors, so no symlink is followed at any level;
* identify directories by device and inode, and delete one only if it is still
  the directory that was recorded when it was created;
* delete with ``shutil.rmtree`` relative to a directory file descriptor. On
  platforms where that is symlink-attack resistant it never follows links
  inside the tree.

Deciding *whether* a directory may be deleted (it belongs to this execution, as
recorded in the resource ledger) is the caller's job (#11); these helpers make
the deletion itself safe.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from ilmar.domain.errors import OwnershipConflictError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """Immutable identity of a directory: survives renames, changes if replaced."""

    device: int
    inode: int


def ensure_directory(path: Path, *, root: Path) -> DirectoryIdentity:
    """Create ``path`` and any missing parents below ``root``; return its identity.

    Raises ``OwnershipConflictError`` if any component is a symlink or not a
    directory, and ``ValueError`` if ``path`` is not strictly inside ``root``.
    """
    parts = _relative_parts(path, root)
    fd = _open_root(root)
    try:
        for name in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=fd)
            child = _open_child(fd, name, path)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        return DirectoryIdentity(info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def directory_identity(path: Path, *, root: Path) -> DirectoryIdentity | None:
    """The identity of ``path``, or ``None`` if it does not exist."""
    parts = _relative_parts(path, root)
    fd = _open_root(root)
    try:
        for name in parts:
            try:
                child = _open_child(fd, name, path)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        return DirectoryIdentity(info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def remove_directory(path: Path, *, root: Path, expected: DirectoryIdentity) -> bool:
    """Delete the tree at ``path`` if it is still the ``expected`` directory.

    Returns ``False`` if it no longer exists (idempotent). Raises
    ``OwnershipConflictError``, deleting nothing, if ``path`` or any parent is a
    symlink or not a directory, or if the directory was replaced.
    """
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError("this platform cannot delete directory trees safely")
    parts = _relative_parts(path, root)
    *parents, last = parts
    fd = _open_root(root)
    try:
        for name in parents:
            try:
                child = _open_child(fd, name, path)
            except FileNotFoundError:
                return False
            os.close(fd)
            fd = child
        try:
            info = os.stat(last, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            raise OwnershipConflictError(f"{path} is not a plain directory; not deleting it")
        if DirectoryIdentity(info.st_dev, info.st_ino) != expected:
            raise OwnershipConflictError(f"{path} was replaced since it was recorded")
        shutil.rmtree(last, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    if not path.is_absolute() or not root.is_absolute():
        raise ValueError("paths must be absolute")
    if ".." in path.parts or path != Path(*path.parts):
        raise ValueError(f"path must be normalised: {path}")
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"{path} is not strictly inside {root}")
    return path.relative_to(root).parts


def _open_root(root: Path) -> int:
    try:
        return os.open(root, _DIR_FLAGS)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise OwnershipConflictError(f"root {root} is not a plain directory") from exc
        raise


def _open_child(parent_fd: int, name: str, full: Path) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise OwnershipConflictError(
                f"{name!r} in {full} is a symlink or not a directory"
            ) from exc
        raise
