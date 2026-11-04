# SPDX-License-Identifier: Apache-2.0
"""Symlink-safe directory creation and deletion (ADR-0012)."""

from __future__ import annotations

import os
import resource
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.core import safe_fs
from ilmar.core.safe_fs import (
    DirectoryIdentity,
    directory_identity,
    ensure_directory,
    remove_directory,
)
from ilmar.domain.errors import OwnershipConflictError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "workspaces"
    path.mkdir()
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "host-data"
    path.mkdir()
    (path / "precious.txt").write_text("must survive\n")
    return path


def test_ensure_creates_nested_directories_and_is_idempotent(root: Path) -> None:
    target = root / "exec" / "1" / "agent"
    first = ensure_directory(target, root=root)
    second = ensure_directory(target, root=root)
    assert target.is_dir()
    assert first == second == directory_identity(target, root=root)


def test_ensure_refuses_symlinked_components(root: Path, outside: Path) -> None:
    (root / "exec").symlink_to(outside)
    with pytest.raises(OwnershipConflictError, match="symlink"):
        ensure_directory(root / "exec" / "1", root=root)
    assert not (outside / "1").exists()


def test_ensure_refuses_files_in_the_way(root: Path) -> None:
    (root / "exec").write_text("not a dir")
    with pytest.raises(OwnershipConflictError):
        ensure_directory(root / "exec" / "1", root=root)


@pytest.mark.parametrize("relative", ["", "../escape", "a/../../escape"])
def test_paths_must_be_strictly_inside_root(root: Path, relative: str) -> None:
    with pytest.raises(ValueError, match=r"inside|normalised"):
        ensure_directory(Path(f"{root}/{relative}") if relative else root, root=root)


def test_identity_of_missing_directory_is_none(root: Path) -> None:
    assert directory_identity(root / "missing" / "deeper", root=root) is None


def test_remove_deletes_the_recorded_tree(root: Path) -> None:
    target = root / "exec" / "1"
    ensure_directory(target / "agent" / "src", root=root)
    identity = directory_identity(target, root=root)
    assert identity is not None
    (target / "agent" / "src" / "file.py").write_text("x")
    assert remove_directory(target, root=root, expected=identity)
    assert not target.exists()
    assert (root / "exec").is_dir()


def test_remove_is_idempotent(root: Path) -> None:
    target = root / "exec" / "1"
    identity = ensure_directory(target, root=root)
    assert remove_directory(target, root=root, expected=identity)
    assert not remove_directory(target, root=root, expected=identity)
    assert not remove_directory(root / "never" / "existed", root=root, expected=identity)


def test_remove_never_follows_symlinks_inside_the_tree(root: Path, outside: Path) -> None:
    target = root / "exec" / "1"
    identity = ensure_directory(target, root=root)
    (target / "to-dir").symlink_to(outside)
    (target / "to-file").symlink_to(outside / "precious.txt")
    nested = target / "agent"
    nested.mkdir()
    (nested / "deep-link").symlink_to(outside)
    assert remove_directory(target, root=root, expected=identity)
    assert not target.exists()
    assert (outside / "precious.txt").read_text() == "must survive\n"


def test_remove_refuses_when_the_target_is_a_symlink(root: Path, outside: Path) -> None:
    (root / "exec").mkdir()
    target = root / "exec" / "1"
    target.symlink_to(outside)
    fake = DirectoryIdentity(outside.stat().st_dev, outside.stat().st_ino)
    with pytest.raises(OwnershipConflictError, match="symlink"):
        remove_directory(target, root=root, expected=fake)
    assert (outside / "precious.txt").exists()
    assert target.is_symlink()


def test_remove_refuses_when_a_parent_is_a_symlink(root: Path, outside: Path) -> None:
    (outside / "1").mkdir()
    (root / "exec").symlink_to(outside)
    real = outside / "1"
    identity = DirectoryIdentity(real.stat().st_dev, real.stat().st_ino)
    with pytest.raises(OwnershipConflictError, match="symlink"):
        remove_directory(root / "exec" / "1", root=root, expected=identity)
    assert real.is_dir()
    assert (outside / "precious.txt").exists()


def test_remove_refuses_a_replaced_directory(root: Path) -> None:
    target = root / "exec" / "1"
    recorded = ensure_directory(target, root=root)
    (target / "keep.txt").write_text("x")
    target.rename(root / "exec" / "moved-away")
    ensure_directory(target, root=root)
    (target / "someone-elses.txt").write_text("not ours")
    with pytest.raises(OwnershipConflictError, match="replaced"):
        remove_directory(target, root=root, expected=recorded)
    assert (target / "someone-elses.txt").exists()


def test_remove_refuses_a_file(root: Path) -> None:
    (root / "exec").mkdir()
    (root / "exec" / "1").write_text("file")
    with pytest.raises(OwnershipConflictError):
        remove_directory(root / "exec" / "1", root=root, expected=DirectoryIdentity(0, 0))


def test_root_itself_can_never_be_removed(root: Path) -> None:
    identity = DirectoryIdentity(root.stat().st_dev, root.stat().st_ino)
    with pytest.raises(ValueError, match="inside"):
        remove_directory(root, root=root, expected=identity)
    assert root.is_dir()


def test_root_must_not_be_a_symlink(tmp_path: Path, outside: Path) -> None:
    link = tmp_path / "link-root"
    link.symlink_to(outside)
    with pytest.raises(OwnershipConflictError, match="root"):
        ensure_directory(link / "x", root=link)


# -- adversarial races (PR #37 review) ----------------------------------------


def test_swap_after_verification_cannot_redirect_deletion(
    root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verified directory is renamed away and a foreign one put in its place
    between the identity check and the deletion: the foreign one survives."""
    target = root / "exec" / "1"
    identity = ensure_directory(target, root=root)
    (target / "ours.txt").write_text("ours\n")
    real_clear = safe_fs._clear

    def swap_then_clear(fd: int) -> None:
        target.rename(root / "exec" / "moved")
        target.mkdir()
        (target / "foreign.txt").write_text("not ours\n")
        real_clear(fd)

    monkeypatch.setattr(safe_fs, "_clear", swap_then_clear)
    with pytest.raises(OwnershipConflictError, match="replaced during deletion"):
        remove_directory(target, root=root, expected=identity)
    assert (target / "foreign.txt").read_text() == "not ours\n"
    assert list((root / "exec" / "moved").iterdir()) == []  # only the verified inode was cleared


def test_swap_to_symlink_after_verification_deletes_nothing_outside(
    root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = root / "exec" / "1"
    identity = ensure_directory(target, root=root)
    real_clear = safe_fs._clear

    def swap_then_clear(fd: int) -> None:
        target.rename(root / "exec" / "moved")
        target.symlink_to(outside)
        real_clear(fd)

    monkeypatch.setattr(safe_fs, "_clear", swap_then_clear)
    with pytest.raises(OwnershipConflictError, match="replaced during deletion"):
        remove_directory(target, root=root, expected=identity)
    assert (outside / "precious.txt").read_text() == "must survive\n"
    assert target.is_symlink()


def test_subdirectory_swapped_for_symlink_mid_deletion_is_not_followed(
    root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = root / "exec" / "1"
    identity = ensure_directory(target / "sub", root=root)
    (target / "sub" / "file.txt").write_text("x")
    real_open_child = safe_fs._open_child

    def swap_then_open(parent_fd: int, name: str, full: Path) -> int:
        if name == "sub" and (target / "sub").is_dir() and not (target / "sub").is_symlink():
            (target / "sub").rename(target / "sub-moved")
            (target / "sub").symlink_to(outside)
        return real_open_child(parent_fd, name, full)

    monkeypatch.setattr(safe_fs, "_open_child", swap_then_open)
    with pytest.raises(OwnershipConflictError):
        remove_directory(target, root=root, expected=identity)
    assert (outside / "precious.txt").read_text() == "must survive\n"


DEPTH = 1500  # deeper than Python's recursion limit; one descriptor per level


@pytest.fixture
def enough_descriptors() -> Iterator[None]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = DEPTH + 100
    if soft >= needed:
        yield
        return
    if hard != resource.RLIM_INFINITY and hard < needed:
        pytest.skip(f"needs {needed} file descriptors, hard limit is {hard}")
    resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.usefixtures("enough_descriptors")
def test_very_deep_trees_are_deleted_without_recursion(root: Path) -> None:
    target = root / "exec" / "1"
    identity = ensure_directory(target, root=root)
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    for _ in range(DEPTH):
        os.mkdir("d", dir_fd=fd)
        child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(fd)
        fd = child
    leaf = os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
    os.close(leaf)
    os.close(fd)
    assert remove_directory(target, root=root, expected=identity)
    assert not target.exists()
