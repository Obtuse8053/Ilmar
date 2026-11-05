# SPDX-License-Identifier: Apache-2.0
"""Single-instance ownership: the instance lock plus the store's fence (ADR-0012, ADR-0020).

Two independent mechanisms, so that losing either stops all side effects:

1. **The instance lock.** An exclusive ``flock`` on
   ``<state>/coordinator/instance.lock``, held through one open descriptor for
   the coordinator's whole life. Only the kernel releases it, when the
   process exits. A second coordinator on the same state directory cannot
   start (``InstanceLockedError``). :meth:`InstanceLock.verify` also detects
   the lock *file* being deleted or replaced: someone else could then lock
   the new file.
2. **The fence.** Taking ownership increments the store's epoch
   (``ExecutionStore.claim_instance``). Every write carries the fence, and
   the store refuses a superseded one. A coordinator that lost the lock for
   any reason, once another coordinator claimed the store, can write nothing,
   and therefore records no intent and performs no further side effect.

:meth:`Ownership.check` verifies both, and the journal calls it immediately
before every external side effect.

Inside one coordinator, :class:`ExecutionLeases` gives each execution at most
one worker at a time (ADR-0012 fencing level 2). Leadership is per instance,
work is per execution: different executions can run in parallel, and nothing
here serialises them.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import threading
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import ensure_directory, require_canonical_root
from ilmar.domain.errors import (
    ConflictError,
    FencingError,
    InstanceLockedError,
    OwnershipConflictError,
)
from ilmar.domain.ledger import Fence
from ilmar.ports.execution_store import ExecutionStore

LOCK_NAME = "instance.lock"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC


class InstanceLock:
    """An exclusive lock on one state directory, held until :meth:`release`."""

    def __init__(self, path: Path, fd: int, identity: tuple[int, int]) -> None:
        self.path = path
        self._fd: int | None = fd
        self._identity = identity

    @classmethod
    def acquire(cls, layout: WorkspaceLayout) -> InstanceLock:
        """Take the lock, or raise ``InstanceLockedError`` if another process holds it.

        The state directory must be a canonical path (no symlinks), and the
        lock file is opened without following a symlink.
        """
        state_dir = layout.coordinator_root.parent
        require_canonical_root(state_dir)
        ensure_directory(layout.coordinator_root, root=state_dir)
        directory = os.open(layout.coordinator_root, _DIR_FLAGS)
        try:
            try:
                fd = os.open(LOCK_NAME, _LOCK_FLAGS, 0o600, dir_fd=directory)
            except OSError as error:
                raise OwnershipConflictError(
                    f"cannot open the instance lock (symlink or not a file): {error.strerror}"
                ) from None
        finally:
            os.close(directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OwnershipConflictError("the instance lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstanceLockedError(
                    "another coordinator holds this state directory's instance lock"
                ) from None
            # Diagnostics only; ownership is the lock itself, never this text.
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"pid={os.getpid()}\n".encode(), 0)
        except BaseException:
            os.close(fd)
            raise
        return cls(layout.coordinator_root / LOCK_NAME, fd, (info.st_dev, info.st_ino))

    @property
    def held(self) -> bool:
        return self._fd is not None

    def verify(self) -> None:
        """Raise ``FencingError`` unless the lock is held on the file at its path."""
        if self._fd is None:
            raise FencingError("the instance lock was released")
        try:
            on_disk = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            raise FencingError("the instance lock file was removed") from None
        if (on_disk.st_dev, on_disk.st_ino) != self._identity:
            raise FencingError("the instance lock file was replaced")

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # closing the descriptor releases the flock


class Ownership:
    """This coordinator's ownership: the instance lock and the store's fence."""

    def __init__(self, lock: InstanceLock, store: ExecutionStore, fence: Fence) -> None:
        self._lock = lock
        self._store = store
        self.fence = fence

    @classmethod
    def take(
        cls,
        *,
        layout: WorkspaceLayout,
        store: ExecutionStore,
        instance_id: str,
        now: datetime,
    ) -> Ownership:
        """Lock the instance, then claim the store with a new epoch.

        If claiming fails, the lock is released before the error propagates.
        """
        lock = InstanceLock.acquire(layout)
        try:
            fence = store.claim_instance(instance_id, now=now)
        except BaseException:
            lock.release()
            raise
        return cls(lock, store, fence)

    def check(self) -> None:
        """Raise ``FencingError`` unless this coordinator still owns everything.

        Call immediately before each external side effect.
        """
        self._lock.verify()
        if self._store.fence() != self.fence:
            raise FencingError(
                "another coordinator took ownership of the store; no further side effects"
            )

    def release(self) -> None:
        with contextlib.suppress(OSError):
            self._lock.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class ExecutionLease:
    """The right of one worker to act for one execution, until released."""

    def __init__(self, leases: ExecutionLeases, execution_id: str) -> None:
        self.execution_id = execution_id
        self._leases = leases
        self._held = True

    def check(self, execution_id: str) -> None:
        """Raise ``FencingError`` unless this lease is held and is for ``execution_id``."""
        if not self._held or not self._leases.holds(self):
            raise FencingError(f"the lease on {self.execution_id} is no longer held")
        if execution_id != self.execution_id:
            raise FencingError(f"a lease on {self.execution_id} cannot act for {execution_id}")

    def release(self) -> None:
        if self._held:
            self._held = False
            self._leases.release(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class ExecutionLeases:
    """In-process registry: at most one lease per execution id. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, ExecutionLease] = {}

    def acquire(self, execution_id: str) -> ExecutionLease:
        """Lease ``execution_id``, or raise ``ConflictError`` if another worker holds it."""
        with self._lock:
            if execution_id in self._held:
                raise ConflictError(f"another worker holds the lease on {execution_id}")
            lease = ExecutionLease(self, execution_id)
            self._held[execution_id] = lease
            return lease

    def holds(self, lease: ExecutionLease) -> bool:
        with self._lock:
            return self._held.get(lease.execution_id) is lease

    def release(self, lease: ExecutionLease) -> None:
        with self._lock:
            if self._held.get(lease.execution_id) is lease:
                del self._held[lease.execution_id]
