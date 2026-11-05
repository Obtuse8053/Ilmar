# SPDX-License-Identifier: Apache-2.0
"""Instance lock and ownership (#11a, ADR-0020)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import LOCK_NAME, InstanceLock, Ownership
from ilmar.domain.errors import FencingError, InstanceLockedError, OwnershipConflictError

from helpers import at

SRC = str(Path(__file__).resolve().parents[2] / "src")
PROBE = """
import sys
from pathlib import Path
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import InstanceLock
from ilmar.domain.errors import InstanceLockedError
try:
    InstanceLock.acquire(WorkspaceLayout.under(Path(sys.argv[1])))
except InstanceLockedError:
    sys.exit(3)
sys.exit(0)
"""


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    return WorkspaceLayout.under(state)


def other_process_can_lock(layout: WorkspaceLayout) -> bool:
    done = subprocess.run(  # noqa: S603 - a fixed program
        [sys.executable, "-c", PROBE, str(layout.coordinator_root.parent)],
        env={"PYTHONPATH": SRC, "PATH": os.environ.get("PATH", "")},
        check=False,
        timeout=60,
    )
    assert done.returncode in (0, 3)
    return done.returncode == 0


def test_a_second_process_cannot_take_the_lock(layout: WorkspaceLayout) -> None:
    lock = InstanceLock.acquire(layout)
    assert not other_process_can_lock(layout)
    lock.release()
    assert other_process_can_lock(layout)


def test_a_second_lock_in_the_same_process_is_refused(layout: WorkspaceLayout) -> None:
    lock = InstanceLock.acquire(layout)
    with pytest.raises(InstanceLockedError):
        InstanceLock.acquire(layout)
    lock.release()


def test_a_replaced_or_removed_lock_file_is_detected(layout: WorkspaceLayout) -> None:
    lock = InstanceLock.acquire(layout)
    lock.verify()
    path = layout.coordinator_root / LOCK_NAME
    path.unlink()
    with pytest.raises(FencingError, match="removed"):
        lock.verify()
    path.write_text("")  # someone else could now lock this new file
    with pytest.raises(FencingError, match="replaced"):
        lock.verify()
    lock.release()
    with pytest.raises(FencingError, match="released"):
        lock.verify()


def test_a_symlinked_lock_file_is_refused(layout: WorkspaceLayout, tmp_path: Path) -> None:
    layout.coordinator_root.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text("")
    (layout.coordinator_root / LOCK_NAME).symlink_to(target)
    with pytest.raises(OwnershipConflictError, match="symlink"):
        InstanceLock.acquire(layout)


def test_a_symlinked_state_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path.resolve() / "link"
    link.symlink_to(real)
    with pytest.raises(OwnershipConflictError, match="canonical"):
        InstanceLock.acquire(WorkspaceLayout.under(link))


def test_taking_ownership_locks_and_claims(layout: WorkspaceLayout, tmp_path: Path) -> None:
    with SQLiteExecutionStore(tmp_path / "db.sqlite3") as store:
        with Ownership.take(layout=layout, store=store, instance_id="inst", now=at(0)) as first:
            first.check()
            assert first.fence == store.fence()
            assert not other_process_can_lock(layout)
        with Ownership.take(layout=layout, store=store, instance_id="inst", now=at(1)) as second:
            assert second.fence.epoch == first.fence.epoch + 1
            with pytest.raises(FencingError):
                first.check()


def test_a_failed_claim_releases_the_lock(layout: WorkspaceLayout, tmp_path: Path) -> None:
    with SQLiteExecutionStore(tmp_path / "db.sqlite3") as store:
        store.claim_instance("someone-else", now=at(0))
        with pytest.raises(FencingError, match="belongs to instance"):
            Ownership.take(layout=layout, store=store, instance_id="inst", now=at(1))
        assert other_process_can_lock(layout)


def test_ownership_is_lost_when_another_coordinator_claims_the_store(
    layout: WorkspaceLayout, tmp_path: Path
) -> None:
    """Even if the lock was somehow bypassed, the fence stops the old owner."""
    path = tmp_path / "db.sqlite3"
    with SQLiteExecutionStore(path) as store, SQLiteExecutionStore(path) as intruder:
        owner = Ownership.take(layout=layout, store=store, instance_id="inst", now=at(0))
        intruder.claim_instance("inst", now=at(1))
        with pytest.raises(FencingError, match="another coordinator"):
            owner.check()
        owner.release()
