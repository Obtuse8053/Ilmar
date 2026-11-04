# SPDX-License-Identifier: Apache-2.0
"""Create, verify and delete host directories without following symlinks (ADR-0012).

Workspaces are written by untrusted agents, and their directories may contain
symlinks pointing anywhere on the host. These helpers:

* walk from a canonical root one component at a time using directory file
  descriptors opened with ``O_NOFOLLOW``, so no symlink is followed at any
  level;
* identify directories by device and inode;
* delete through file descriptors bound to the verified directory (see
  :func:`remove_directory`), never by re-resolving a path.

**Threat model.** Components *above* a workspace (the root, ``<execution>/``
and ``<attempt>/``) are created by the coordinator with mode ``0700``, and no
sandbox ever mounts them. Only a host-level actor with write access to the
state directory could change them, and that is outside the threat model.
Agents control only the *contents* of the directory mounted into their
sandbox. Deciding *whether* a directory may be deleted or mounted (it belongs
to this execution, as the resource ledger records) is the caller's job (#11).
"""

from __future__ import annotations

import contextlib
import errno
import os
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


def require_canonical_root(root: Path) -> None:
    """Refuse a root that is, or lies beneath, a symlink.

    Opening each component with ``O_NOFOLLOW`` only protects components
    *below* the root. The root itself must therefore be its own canonical path.
    """
    if not root.is_absolute() or os.path.realpath(root) != str(root):
        raise OwnershipConflictError(f"root {root} is not a canonical path (symlink involved)")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise OwnershipConflictError(f"root {root} is not a directory")


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
        return _identity(fd)
    finally:
        os.close(fd)


def directory_identity(path: Path, *, root: Path) -> DirectoryIdentity | None:
    """The identity of ``path``, or ``None`` if it does not exist."""
    try:
        fd = _open_path(path, root)
    except FileNotFoundError:
        return None
    try:
        return _identity(fd)
    finally:
        os.close(fd)


def verify_directory(path: Path, *, root: Path, expected: DirectoryIdentity) -> None:
    """Check that ``path`` is reachable from ``root`` without symlinks and is ``expected``.

    Used immediately before mounting (see :func:`ilmar.core.mounts.authorize_mount`).
    Raises ``OwnershipConflictError`` on a symlink, a non-directory, a missing
    directory or a different identity.
    """
    require_canonical_root(root)
    try:
        fd = _open_path(path, root)
    except FileNotFoundError as exc:
        raise OwnershipConflictError(f"{path} does not exist") from exc
    try:
        if _identity(fd) != expected:
            raise OwnershipConflictError(f"{path} is not the recorded directory")
    finally:
        os.close(fd)


def remove_directory(path: Path, *, root: Path, expected: DirectoryIdentity) -> bool:
    """Delete the directory at ``path`` if it is the ``expected`` directory.

    Returns ``False`` if nothing exists at ``path`` (idempotent). The deletion
    is bound to the verified directory, not to its name:

    1. The directory is opened with ``O_NOFOLLOW``, and *that open descriptor*
       is checked against ``expected``. A swap before the open is detected; a
       swap after it cannot redirect the descriptor.
    2. All contents are deleted relative to descriptors of verified
       directories. Each subdirectory is opened with ``O_NOFOLLOW`` and checked
       against the entry that was listed, so symlinks are unlinked as links and
       never followed.
    3. Finally the now-empty directory's *name* is removed with ``rmdir``. If
       the name was swapped after step 1, ``rmdir`` can only remove an empty
       directory, and fails on anything else. Content outside the verified
       directory is never deleted. In that case ``OwnershipConflictError`` is
       raised.
    """
    parts = _relative_parts(path, root)
    *parents, last = parts
    parent_fd = _open_root(root)
    try:
        for name in parents:
            try:
                child = _open_child(parent_fd, name, path)
            except FileNotFoundError:
                return False
            os.close(parent_fd)
            parent_fd = child
        try:
            target_fd = _open_child(parent_fd, last, path)
        except FileNotFoundError:
            return False
        try:
            if _identity(target_fd) != expected:
                raise OwnershipConflictError(f"{path} was replaced since it was recorded")
            _clear(target_fd)
        finally:
            os.close(target_fd)
        try:
            os.rmdir(last, dir_fd=parent_fd)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                raise OwnershipConflictError(
                    f"{path} was replaced during deletion; the replacement was left untouched"
                ) from exc
            raise
        return True
    finally:
        os.close(parent_fd)


def _clear(dir_fd: int) -> None:
    """Delete everything inside the directory open at ``dir_fd``, iteratively.

    Every level is reached through a descriptor of a directory whose identity
    matched the entry that was listed. Files and symlinks are unlinked as
    entries. Emptied subdirectories are removed with ``rmdir``, which can only
    ever remove an empty directory.

    One descriptor is held per level of depth. A tree deeper than the process's
    descriptor limit makes deletion fail with ``OSError`` (``EMFILE``),
    leaving the rest in place: it fails safe, never unsafe.
    """
    stack: list[tuple[int, list[str], str]] = [(dir_fd, os.listdir(dir_fd), "")]
    try:
        while stack:
            fd, names, own_name = stack[-1]
            if not names:
                stack.pop()
                if stack:
                    os.close(fd)
                    os.rmdir(own_name, dir_fd=stack[-1][0])
                continue
            name = names.pop()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                os.unlink(name, dir_fd=fd)
                continue
            child = _open_child(fd, name, Path(name))
            if _identity(child) != DirectoryIdentity(info.st_dev, info.st_ino):
                os.close(child)
                raise OwnershipConflictError(f"{name!r} changed while being deleted")
            stack.append((child, os.listdir(child), name))
    finally:
        for fd, _, _ in stack[1:]:
            os.close(fd)


def _open_path(path: Path, root: Path) -> int:
    fd = _open_root(root)
    try:
        for name in _relative_parts(path, root):
            child = _open_child(fd, name, path)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _identity(fd: int) -> DirectoryIdentity:
    info = os.fstat(fd)
    return DirectoryIdentity(info.st_dev, info.st_ino)


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
