# SPDX-License-Identifier: Apache-2.0
"""The write-ahead journal: idempotency, fencing and crash recovery (#11a, ADR-0020).

Crash injection: :class:`CrashingStore` wraps a real SQLite store. From a
chosen commit on, it raises :class:`Crash` (a ``BaseException``, like the
process dying) and refuses every further write, so nothing after the crash
point is persisted. A "restart" opens a new store on the same file and takes
ownership again.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.journal import Journal, check_candidate, freeze_result
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import LOCK_NAME, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import (
    AmbiguousOperationError,
    FencingError,
    LedgerIntegrityError,
    ProviderError,
)
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Commit, Fence, OperationKind, OperationStatus
from ilmar.domain.tasks import TaskRef

from helpers import at

S = ExecutionState
K = OperationKind
ST = OperationStatus
MACHINE = ExecutionStateMachine(max_attempts=3)
BASE, CANDIDATE = "a" * 40, "b" * 40


class Crash(BaseException):
    """The process died here."""


class CrashingStore:
    """Delegates to a real store; from commit number ``crash_at`` on, every write crashes."""

    def __init__(self, inner: SQLiteExecutionStore, crash_at: int | None = None) -> None:
        self.inner = inner
        self.crash_at = crash_at
        self.commits = 0
        self.crashed = False
        self.after_commit: Callable[[], None] | None = None

    def commit(self, fence: Fence, changes: Commit) -> None:
        self.commits += 1
        if self.crashed or (self.crash_at is not None and self.commits >= self.crash_at):
            self.crashed = True
            raise Crash(f"crash at commit {self.commits}")
        self.inner.commit(fence, changes)
        if self.after_commit is not None:
            hook, self.after_commit = self.after_commit, None
            hook()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class World:
    """The external systems, as the effects see them."""

    claimed: set[str] = field(default_factory=set)
    pushes: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)

    def claim(self) -> Mapping[str, str]:
        self.effects.append("claim")
        self.claimed.add("task-1")  # idempotent, like ADR-0013 claims
        return {"task": "task-1"}

    def push(self) -> Mapping[str, str]:
        self.effects.append("push")
        self.pushes.append(CANDIDATE)
        return {"head": CANDIDATE}


@dataclass
class Coordinator:
    store: CrashingStore
    ownership: Ownership
    journal: Journal

    def execution(self) -> Execution:
        found = self.store.inner.get("e1")
        assert found is not None
        return found


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    return WorkspaceLayout.under(state)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "executions.sqlite3"


def start(layout: WorkspaceLayout, path: Path, *, crash_at: int | None = None) -> Coordinator:
    store = CrashingStore(SQLiteExecutionStore(path), crash_at)
    ownership = Ownership.take(layout=layout, store=store.inner, instance_id="inst", now=at(0))
    journal = Journal(store=store, ownership=ownership, clock=lambda: at(10))
    return Coordinator(store, ownership, journal)


def stop(coordinator: Coordinator) -> None:
    """The process exits: the kernel releases the lock, the connection closes."""
    coordinator.ownership.release()
    coordinator.store.inner.close()


@pytest.fixture
def coordinator(layout: WorkspaceLayout, path: Path) -> Iterator[Coordinator]:
    c = start(layout, path)
    execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(1))
    c.store.inner.create(execution, transition, fence=c.ownership.fence)
    yield c
    stop(c)


# -- write-ahead and idempotency ---------------------------------------------------------


def test_the_intent_is_durable_before_the_effect_runs(coordinator: Coordinator) -> None:
    seen: list[Any] = []

    def effect() -> Mapping[str, str]:
        seen.append(coordinator.store.inner.operation("claim-task:e1:1"))
        return {"ok": "1"}

    coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), effect)
    assert seen[0] is not None
    assert seen[0].status is ST.INTENDED


def test_a_succeeded_step_is_never_performed_twice(coordinator: Coordinator) -> None:
    world = World()
    first = coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push)
    second = coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push)
    assert first == second == {"head": CANDIDATE}
    assert world.pushes == [CANDIDATE]


def test_a_definitive_failure_is_recorded_and_not_retried_in_the_attempt(
    coordinator: Coordinator,
) -> None:
    def refused() -> Mapping[str, str]:
        raise ProviderError("the remote said no")

    with pytest.raises(ProviderError):
        coordinator.journal.perform(
            K.PUBLISH_REVISION, coordinator.execution(), refused, definitive=(ProviderError,)
        )
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert operation.status is ST.FAILED
    assert operation.note == "ProviderError"  # the type only, never the message
    with pytest.raises(LedgerIntegrityError, match="already failed"):
        coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), World().push)


def test_an_unclear_failure_is_ambiguous_and_blocks_until_reconciled(
    coordinator: Coordinator,
) -> None:
    def timed_out() -> Mapping[str, str]:
        raise TimeoutError("no answer")

    with pytest.raises(TimeoutError):
        coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), timed_out)
    assert coordinator.store.inner.operation("publish-revision:e1:1").status is ST.AMBIGUOUS  # type: ignore[union-attr]
    world = World()
    with pytest.raises(AmbiguousOperationError) as info:
        coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push)
    assert info.value.key == "publish-revision:e1:1"
    assert world.pushes == []
    coordinator.journal.reconcile(
        info.value.key,
        ST.SUCCEEDED,
        reason="branch head equals the candidate",
        result={"head": CANDIDATE},
    )
    assert coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push) == {
        "head": CANDIDATE
    }
    assert world.pushes == []


def test_a_repeatable_step_is_repeated_after_an_unclear_failure(
    coordinator: Coordinator,
) -> None:
    calls = []

    def flaky() -> Mapping[str, str]:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError
        return {"task": "task-1"}

    with pytest.raises(TimeoutError):
        coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), flaky)
    assert coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), flaky) == {
        "task": "task-1"
    }
    assert len(calls) == 2


def test_reconciliation_only_settles_unfinished_operations(coordinator: Coordinator) -> None:
    coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), World().claim)
    with pytest.raises(LedgerIntegrityError, match="already succeeded"):
        coordinator.journal.reconcile("claim-task:e1:1", ST.FAILED, reason="x")
    with pytest.raises(LedgerIntegrityError, match="no operation"):
        coordinator.journal.reconcile("claim-task:e1:9", ST.FAILED, reason="x")
    with pytest.raises(ValueError, match="succeeded or failed"):
        coordinator.journal.reconcile("claim-task:e1:1", ST.AMBIGUOUS, reason="x")


# -- stale coordinators ------------------------------------------------------------------


def test_a_superseded_coordinator_records_no_intent_and_acts_not(
    coordinator: Coordinator, path: Path
) -> None:
    with SQLiteExecutionStore(path) as intruder:
        intruder.claim_instance("inst", now=at(5))
    world = World()
    with pytest.raises(FencingError):
        coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push)
    assert world.effects == []
    assert coordinator.store.inner.operation("publish-revision:e1:1") is None


def test_losing_ownership_after_the_intent_stops_the_effect(
    coordinator: Coordinator, path: Path
) -> None:
    """The check right before the effect catches a takeover after the intent was written."""

    def takeover() -> None:
        with SQLiteExecutionStore(path) as intruder:
            intruder.claim_instance("inst", now=at(5))

    coordinator.store.after_commit = takeover
    world = World()
    with pytest.raises(FencingError):
        coordinator.journal.perform(K.PUBLISH_REVISION, coordinator.execution(), world.push)
    assert world.effects == []
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert operation.status is ST.INTENDED  # the next owner must reconcile it


def test_a_replaced_lock_file_stops_the_effect(
    coordinator: Coordinator, layout: WorkspaceLayout
) -> None:
    def replace_lock() -> None:
        lock = layout.coordinator_root / LOCK_NAME
        lock.unlink()
        lock.write_text("")

    coordinator.store.after_commit = replace_lock
    world = World()
    with pytest.raises(FencingError, match="replaced"):
        coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), world.claim)
    assert world.effects == []


# -- candidate identity ------------------------------------------------------------------


def candidate(paths: tuple[str, ...] = ("src/a.py", "tests/test_a.py")) -> CandidateRevision:
    return CandidateRevision(CANDIDATE, BASE, paths)


def running_agent(c: Coordinator) -> Execution:
    execution = c.execution()
    for target in (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT):
        updated, transition = MACHINE.advance(
            execution,
            target,
            now=at(2),
            reason="t",
            base_revision=BASE if target is S.RUNNING_AGENT else None,
        )
        c.store.inner.apply(updated, transition, fence=c.ownership.fence)
        execution = updated
    return execution


def test_freezing_records_the_candidate_atomically_with_verifying(
    coordinator: Coordinator,
) -> None:
    execution = running_agent(coordinator)
    frozen = candidate()

    def enter_verifying(result: Mapping[str, str]) -> Commit:
        updated, transition = MACHINE.advance(
            execution,
            S.VERIFYING,
            now=at(3),
            reason="frozen",
            candidate_revision=result["revision"],
        )
        return Commit(applied=((updated, transition),))

    coordinator.journal.perform(
        K.FREEZE_CANDIDATE, execution, lambda: freeze_result(frozen), after=enter_verifying
    )
    stored = coordinator.execution()
    assert (stored.state, stored.candidate_revision) == (S.VERIFYING, CANDIDATE)
    freeze = coordinator.store.inner.operation("freeze-candidate:e1:1")
    assert freeze is not None
    check_candidate(frozen, freeze)  # the same candidate passes


@pytest.mark.parametrize(
    "tampered",
    [
        CandidateRevision("c" * 40, BASE, ("src/a.py", "tests/test_a.py")),
        CandidateRevision(CANDIDATE, "d" * 40, ("src/a.py", "tests/test_a.py")),
        CandidateRevision(CANDIDATE, BASE, ("src/a.py",)),  # a changed path went missing
        CandidateRevision(CANDIDATE, BASE, (".github/x.yml", "src/a.py", "tests/test_a.py")),
    ],
)
def test_a_candidate_that_differs_from_its_freeze_is_refused(
    coordinator: Coordinator, tampered: CandidateRevision
) -> None:
    execution = running_agent(coordinator)
    coordinator.journal.perform(K.FREEZE_CANDIDATE, execution, lambda: freeze_result(candidate()))
    freeze = coordinator.store.inner.operation("freeze-candidate:e1:1")
    assert freeze is not None
    with pytest.raises(LedgerIntegrityError, match="does not match"):
        check_candidate(tampered, freeze)


# -- crash injection ---------------------------------------------------------------------

STEPS = (K.CLAIM_TASK, K.PUBLISH_REVISION)  # one repeatable, one that needs reconciling


def drive(c: Coordinator, world: World) -> None:
    for kind in STEPS:
        effect = world.claim if kind is K.CLAIM_TASK else world.push
        c.journal.perform(kind, c.execution(), effect)


def recover(c: Coordinator, world: World) -> None:
    """What #11c will do: settle what is unclear from what the world shows, then continue."""
    for operation in c.store.inner.unfinished_operations():
        if operation.kind.repeatable:
            continue  # simply performed again
        assert operation.kind is K.PUBLISH_REVISION
        if world.pushes:  # observation: the branch head equals the candidate
            c.journal.reconcile(
                operation.key,
                ST.SUCCEEDED,
                reason="head equals candidate",
                result={"head": CANDIDATE},
            )
        else:
            c.journal.reconcile(operation.key, ST.FAILED, reason="head unchanged")
    with contextlib.suppress(LedgerIntegrityError):  # a step failed for this attempt
        drive(c, world)


TOTAL_COMMITS = 4  # an intent and an outcome for each of the two steps


@pytest.mark.parametrize("crash_at", range(1, TOTAL_COMMITS + 1))
def test_a_crash_at_any_commit_never_repeats_a_non_repeatable_effect(
    layout: WorkspaceLayout, path: Path, crash_at: int
) -> None:
    world = World()
    first = start(layout, path, crash_at=crash_at)
    execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(1))
    first.store.inner.create(execution, transition, fence=first.ownership.fence)
    with pytest.raises(Crash):
        drive(first, world)
    stop(first)

    second = start(layout, path)
    assert second.ownership.fence.epoch == first.ownership.fence.epoch + 1
    recover(second, world)
    stored = {o.kind: o for o in second.store.inner.operations("e1")}
    stop(second)

    assert len(world.pushes) <= 1, "the push must never happen twice"
    assert world.claimed == {"task-1"}
    assert not [o for o in stored.values() if not o.status.is_final]
    if world.pushes:
        assert stored[K.PUBLISH_REVISION].status is ST.SUCCEEDED
    else:  # the push never happened: its step failed for this attempt, never "succeeded"
        assert stored[K.PUBLISH_REVISION].status is ST.FAILED


def test_a_crash_after_the_effect_leaves_an_intent_that_recovery_must_settle(
    layout: WorkspaceLayout, path: Path
) -> None:
    world = World()
    first = start(layout, path, crash_at=4)  # the push happened, its outcome was lost
    execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(1))
    first.store.inner.create(execution, transition, fence=first.ownership.fence)
    with pytest.raises(Crash):
        drive(first, world)
    stop(first)
    second = start(layout, path)
    (pending,) = second.store.inner.unfinished_operations()
    assert (pending.kind, pending.status) == (K.PUBLISH_REVISION, ST.INTENDED)
    with pytest.raises(AmbiguousOperationError):
        second.journal.perform(K.PUBLISH_REVISION, second.execution(), world.push)
    assert world.pushes == [CANDIDATE]
    stop(second)
