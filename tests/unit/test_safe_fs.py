# SPDX-License-Identifier: Apache-2.0
"""Symlink-safe directory creation and deletion (ADR-0012)."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    with pytest.raises(OwnershipConflictError, match="not a plain directory"):
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
