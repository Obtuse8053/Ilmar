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
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.journal import Journal, check_candidate, freeze_result
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import LOCK_NAME, ExecutionLease, ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import (
    AmbiguousOperationError,
    ConcurrencyConflictError,
    ConflictError,
    FencingError,
    LedgerIntegrityError,
    ProviderError,
)
from ilmar.domain.events import EventContext
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

    def commit(self, fence: Fence, changes: Commit, *, context: EventContext | None = None) -> None:
        self.commits += 1
        if self.crashed or (self.crash_at is not None and self.commits >= self.crash_at):
            self.crashed = True
            raise Crash(f"crash at commit {self.commits}")
        self.inner.commit(fence, changes, context=context)
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
    leases: ExecutionLeases = field(default_factory=ExecutionLeases)

    def lease(self, execution_id: str = "e1") -> ExecutionLease:
        if not hasattr(self, "_lease"):
            self._lease = self.leases.acquire(execution_id)
        return self._lease

    def perform(
        self,
        kind: OperationKind,
        effect: Callable[[], Mapping[str, str]],
        *,
        execution: Execution | None = None,
        **options: Any,
    ) -> Mapping[str, str]:
        return self.journal.perform(
            kind, execution or self.execution(), effect, lease=self.lease(), **options
        )

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

    coordinator.perform(K.CLAIM_TASK, effect)
    assert seen[0] is not None
    assert seen[0].status is ST.INTENDED


def test_a_succeeded_step_is_never_performed_twice(coordinator: Coordinator) -> None:
    world = World()
    first = coordinator.perform(K.PUBLISH_REVISION, world.push)
    second = coordinator.perform(K.PUBLISH_REVISION, world.push)
    assert first == second == {"head": CANDIDATE}
    assert world.pushes == [CANDIDATE]


def test_a_definitive_failure_is_recorded_and_not_retried_in_the_attempt(
    coordinator: Coordinator,
) -> None:
    def refused() -> Mapping[str, str]:
        raise ProviderError("the remote said no")

    with pytest.raises(ProviderError):
        coordinator.perform(K.PUBLISH_REVISION, refused, definitive=(ProviderError,))
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert operation.status is ST.FAILED
    assert operation.note == "ProviderError"  # the type only, never the message
    with pytest.raises(LedgerIntegrityError, match="already failed"):
        coordinator.perform(K.PUBLISH_REVISION, World().push)


def test_an_unclear_failure_is_ambiguous_and_blocks_until_reconciled(
    coordinator: Coordinator,
) -> None:
    def timed_out() -> Mapping[str, str]:
        raise TimeoutError("no answer")

    with pytest.raises(TimeoutError):
        coordinator.perform(K.PUBLISH_REVISION, timed_out)
    assert coordinator.store.inner.operation("publish-revision:e1:1").status is ST.AMBIGUOUS  # type: ignore[union-attr]
    world = World()
    with pytest.raises(AmbiguousOperationError) as info:
        coordinator.perform(K.PUBLISH_REVISION, world.push)
    assert info.value.key == "publish-revision:e1:1"
    assert world.pushes == []
    coordinator.journal.reconcile(
        info.value.key,
        ST.SUCCEEDED,
        reason="branch head equals the candidate",
        observed="branch-at-candidate",
        result={"head": CANDIDATE},
        lease=coordinator.lease(),
    )
    assert coordinator.perform(K.PUBLISH_REVISION, world.push) == {"head": CANDIDATE}
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
        coordinator.perform(K.CLAIM_TASK, flaky)
    assert coordinator.perform(K.CLAIM_TASK, flaky) == {"task": "task-1"}
    assert len(calls) == 2


def test_reconciliation_only_settles_unfinished_operations(coordinator: Coordinator) -> None:
    coordinator.perform(K.CLAIM_TASK, World().claim)
    with pytest.raises(LedgerIntegrityError, match="already succeeded"):
        coordinator.journal.reconcile(
            "claim-task:e1:1",
            ST.FAILED,
            reason="x",
            observed="outcome-unknown",
            code="interrupted",
            lease=coordinator.lease(),
        )
    with pytest.raises(LedgerIntegrityError, match="no operation"):
        coordinator.journal.reconcile(
            "claim-task:e1:9",
            ST.FAILED,
            reason="x",
            observed="outcome-unknown",
            code="interrupted",
            lease=coordinator.lease(),
        )
    with pytest.raises(ValueError, match="succeeded or failed"):
        coordinator.journal.reconcile(
            "claim-task:e1:1",
            ST.AMBIGUOUS,
            reason="x",
            observed="outcome-unknown",
            lease=coordinator.lease(),
        )


# -- stale coordinators ------------------------------------------------------------------


def test_a_superseded_coordinator_records_no_intent_and_acts_not(
    coordinator: Coordinator, path: Path
) -> None:
    with SQLiteExecutionStore(path) as intruder:
        intruder.claim_instance("inst", now=at(5))
    world = World()
    with pytest.raises(FencingError):
        coordinator.perform(K.PUBLISH_REVISION, world.push)
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
        coordinator.perform(K.PUBLISH_REVISION, world.push)
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
        coordinator.perform(K.CLAIM_TASK, world.claim)
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

    coordinator.perform(
        K.FREEZE_CANDIDATE,
        execution=execution,
        effect=lambda: freeze_result(frozen),
        after=enter_verifying,
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
    coordinator.perform(
        K.FREEZE_CANDIDATE, execution=execution, effect=lambda: freeze_result(candidate())
    )
    freeze = coordinator.store.inner.operation("freeze-candidate:e1:1")
    assert freeze is not None
    with pytest.raises(LedgerIntegrityError, match="does not match"):
        check_candidate(tampered, freeze)


# -- crash injection ---------------------------------------------------------------------

STEPS = (K.CLAIM_TASK, K.PUBLISH_REVISION)  # one repeatable, one that needs reconciling


def drive(c: Coordinator, world: World) -> None:
    for kind in STEPS:
        effect = world.claim if kind is K.CLAIM_TASK else world.push
        c.perform(kind, effect)


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
                observed="branch-at-candidate",
                result={"head": CANDIDATE},
                lease=c.lease(),
            )
        else:
            c.journal.reconcile(
                operation.key,
                ST.FAILED,
                reason="head unchanged",
                observed="outcome-unknown",
                code="interrupted",
                lease=c.lease(),
            )
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
        second.perform(K.PUBLISH_REVISION, world.push)
    assert world.pushes == [CANDIDATE]
    stop(second)


# -- adversarial review of 0661847 -------------------------------------------------------


def advance(c: Coordinator, target: ExecutionState) -> Execution:
    updated, transition = MACHINE.advance(c.execution(), target, now=at(4), reason="other")
    c.store.inner.apply(updated, transition, fence=c.ownership.fence)
    return updated


def test_a_worker_with_a_stale_view_records_no_intent_and_acts_not(
    coordinator: Coordinator,
) -> None:
    stale = coordinator.execution()
    advance(coordinator, S.CLAIMED)  # someone else moved the execution on
    world = World()
    with pytest.raises(ConcurrencyConflictError, match="stale"):
        coordinator.perform(K.PUBLISH_REVISION, world.push, execution=stale)
    assert world.effects == []
    assert coordinator.store.inner.operation("publish-revision:e1:1") is None


def test_a_change_after_the_intent_stops_the_effect(coordinator: Coordinator) -> None:
    def move_on() -> None:
        advance(coordinator, S.CLAIMED)

    coordinator.store.after_commit = move_on
    world = World()
    with pytest.raises(ConcurrencyConflictError, match="changed after"):
        coordinator.perform(K.PUBLISH_REVISION, world.push)
    assert world.effects == []
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert operation.status is ST.INTENDED


def test_only_the_lease_holder_acts_for_an_execution(coordinator: Coordinator) -> None:
    lease = coordinator.lease()
    with pytest.raises(ConflictError, match="another worker"):
        coordinator.leases.acquire("e1")
    other = coordinator.leases.acquire("e2")
    world = World()
    with pytest.raises(FencingError, match="cannot act for e1"):
        coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), world.claim, lease=other)
    lease.release()
    with pytest.raises(FencingError, match="no longer held"):
        coordinator.journal.perform(K.CLAIM_TASK, coordinator.execution(), world.claim, lease=lease)
    assert world.effects == []
    assert coordinator.leases.acquire("e1") is not lease  # free again after release


def test_executions_run_in_parallel_under_one_coordinator(coordinator: Coordinator) -> None:
    """Leadership is per instance; work is per execution, so nothing serialises them."""
    ids = [f"p{n}" for n in range(6)]
    for n, execution_id in enumerate(ids):
        execution, transition = MACHINE.start(
            TaskRef("fake", f"p{n}"), execution_id=execution_id, now=at(1)
        )
        coordinator.store.inner.create(execution, transition, fence=coordinator.ownership.fence)
    barrier = threading.Barrier(len(ids))
    inside: list[str] = []
    errors: list[BaseException] = []

    def work(execution_id: str) -> None:
        try:
            with coordinator.leases.acquire(execution_id) as lease:
                execution = coordinator.store.inner.get(execution_id)
                assert execution is not None

                def effect() -> Mapping[str, str]:
                    inside.append(execution_id)
                    barrier.wait(timeout=10)  # every worker is inside its effect at once
                    return {"ok": execution_id}

                coordinator.journal.perform(K.CLAIM_TASK, execution, effect, lease=lease)
        except BaseException as error:  # reported below
            errors.append(error)

    threads = [threading.Thread(target=work, args=(i,)) for i in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(inside) == ids
    for execution_id in ids:
        operation = coordinator.store.inner.operation(f"claim-task:{execution_id}:1")
        assert operation is not None
        assert operation.status is ST.SUCCEEDED


def test_an_effect_whose_follow_up_is_refused_keeps_its_result(
    coordinator: Coordinator,
) -> None:
    execution = running_agent(coordinator)
    frozen = freeze_result(candidate())

    def wrong_candidate(result: Mapping[str, str]) -> Commit:
        updated, transition = MACHINE.advance(
            execution, S.VERIFYING, now=at(3), reason="t", candidate_revision="c" * 40
        )
        return Commit(applied=((updated, transition),))

    with pytest.raises(LedgerIntegrityError, match="follow-up changes were refused"):
        coordinator.perform(
            K.FREEZE_CANDIDATE, lambda: frozen, execution=execution, after=wrong_candidate
        )
    operation = coordinator.store.inner.operation("freeze-candidate:e1:1")
    assert operation is not None
    assert operation.status is ST.AMBIGUOUS
    assert dict(operation.result) == frozen  # the knowledge is kept
    assert coordinator.execution().state is S.RUNNING_AGENT  # nothing half-applied


def test_a_failing_follow_up_builder_keeps_the_result_too(coordinator: Coordinator) -> None:
    def broken(result: Mapping[str, str]) -> Commit:
        raise KeyError("missing")

    world = World()
    with pytest.raises(LedgerIntegrityError):
        coordinator.perform(K.PUBLISH_REVISION, world.push, after=broken)
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert (operation.status, dict(operation.result)) == (ST.AMBIGUOUS, {"head": CANDIDATE})
    assert world.pushes == [CANDIDATE]
    with pytest.raises(AmbiguousOperationError):  # never pushed again blindly
        coordinator.perform(K.PUBLISH_REVISION, world.push)


def test_a_freeze_can_never_be_reconciled_into_a_candidate(coordinator: Coordinator) -> None:
    execution = running_agent(coordinator)

    def crash() -> Mapping[str, str]:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        coordinator.perform(K.FREEZE_CANDIDATE, crash, execution=execution)
    with pytest.raises(LedgerIntegrityError, match="never reconciled"):
        coordinator.journal.reconcile(
            "freeze-candidate:e1:1",
            ST.SUCCEEDED,
            reason="trust me",
            observed="outcome-unknown",
            result=freeze_result(candidate()),
            lease=coordinator.lease(),
        )


def test_a_worker_cannot_touch_another_executions_records(coordinator: Coordinator) -> None:
    from ilmar.domain.ledger import Resource, ResourceKind

    execution, transition = MACHINE.start(TaskRef("fake", "2"), execution_id="e2", now=at(1))
    coordinator.store.inner.create(execution, transition, fence=coordinator.ownership.fence)
    foreign = Resource.intended(
        ResourceKind.WORKSPACE, "e2", 1, "/w/e2", owner_token="tok", now=at(2)
    )
    world = World()
    with pytest.raises(LedgerIntegrityError, match="cannot change records of e2"):
        coordinator.perform(K.CLAIM_TASK, world.claim, before=Commit(resources=(foreign,)))
    with pytest.raises(LedgerIntegrityError, match="follow-up changes were refused") as info:
        coordinator.perform(
            K.PUBLISH_REVISION, world.push, after=lambda _: Commit(resources=(foreign,))
        )
    assert "cannot change records of e2" in str(info.value.__cause__)
    assert world.effects == ["push"]  # the claim never ran; the push did, its result was kept
    assert coordinator.store.inner.resources(execution_id="e2") == ()
    publish = coordinator.store.inner.operation("publish-revision:e1:1")
    assert publish is not None
    assert publish.status is ST.AMBIGUOUS


def test_a_lost_fence_on_the_outcome_is_reported_as_lost_ownership(
    coordinator: Coordinator, path: Path
) -> None:
    """Review of 46c1662: it must not be masked as a refused follow-up."""

    def act_then_lose() -> Mapping[str, str]:
        with SQLiteExecutionStore(path) as intruder:
            intruder.claim_instance("inst", now=at(5))
        return {"head": CANDIDATE}

    with pytest.raises(FencingError):
        coordinator.perform(K.PUBLISH_REVISION, act_then_lose)
    operation = coordinator.store.inner.operation("publish-revision:e1:1")
    assert operation is not None
    assert operation.status is ST.INTENDED  # nothing more was written


def test_only_the_lease_holder_reconciles_and_only_while_owning(
    coordinator: Coordinator,
) -> None:
    """Recovery settles operations under the same checks as performing them (review of 28a4a89)."""

    def timed_out() -> Mapping[str, str]:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        coordinator.perform(K.PUBLISH_REVISION, timed_out)
    key = "publish-revision:e1:1"
    other = coordinator.leases.acquire("e2")
    with pytest.raises(FencingError, match="cannot act for e1"):
        coordinator.journal.reconcile(
            key, ST.FAILED, reason="x", observed="outcome-unknown", code="interrupted", lease=other
        )
    lease = coordinator.lease()
    lease.release()
    with pytest.raises(FencingError, match="no longer held"):
        coordinator.journal.reconcile(
            key, ST.FAILED, reason="x", observed="outcome-unknown", code="interrupted", lease=lease
        )
    operation = coordinator.store.inner.operation(key)
    assert operation is not None
    assert operation.status is ST.AMBIGUOUS


# -- instance-scoped operations (ADR-0026 §5, the ADR-0020 amendment) ----------------------

ORPHAN_INTENT = {"task": "fake:1", "claim": "tok", "object": "o-1"}


def perform_orphan(
    c: Coordinator,
    effect: Callable[[], Mapping[str, str]],
    *,
    intent: Mapping[str, str] = ORPHAN_INTENT,
    revalidate: Callable[[], None] = lambda: None,
    **options: Any,
) -> Mapping[str, str]:
    return c.journal.perform_instance(
        K.RELEASE_ORPHAN_CLAIM, effect, step="fake.0123abcd", intent=intent,
        reason="orphaned-claim", revalidate=revalidate, **options,
    )  # fmt: skip


ORPHAN_KEY = "release-orphan-claim:inst:1:fake.0123abcd"


def test_an_instance_operation_is_written_ahead_with_no_execution(
    coordinator: Coordinator,
) -> None:
    seen: list[Any] = []

    def effect() -> Mapping[str, str]:
        seen.append(coordinator.store.inner.operation(ORPHAN_KEY))
        return {"result": "released"}

    assert perform_orphan(coordinator, effect) == {"result": "released"}
    assert seen[0].status is ST.INTENDED
    assert seen[0].execution_id is None
    assert dict(seen[0].intent) == ORPHAN_INTENT
    done = coordinator.store.inner.operation(ORPHAN_KEY)
    assert done is not None
    assert done.status is ST.SUCCEEDED
    events = [e for e in coordinator.store.inner.events() if e.operation == ORPHAN_KEY]
    assert [(e.type.value, e.execution, e.reason) for e in events] == [
        ("operation.intended", None, "orphaned-claim"),
        ("operation.succeeded", None, "orphaned-claim"),
    ]
    assert all(e.pass_id is not None for e in events)


def test_an_instance_operation_records_what_the_effect_found(coordinator: Coordinator) -> None:
    def found(result: Mapping[str, str]) -> str | None:
        return "claim-absent" if result["result"] == "absent" else None

    perform_orphan(coordinator, lambda: {"result": "absent"}, outcome_observed=found)
    (outcome,) = [
        e
        for e in coordinator.store.inner.events()
        if e.operation == ORPHAN_KEY and e.type.value == "operation.succeeded"
    ]
    assert outcome.data["observed"] == "claim-absent"


def test_a_failed_revalidation_never_acts_and_settles_failed(coordinator: Coordinator) -> None:
    world = World()

    def revalidate() -> None:
        raise LedgerIntegrityError("the task is backed again")

    with pytest.raises(LedgerIntegrityError, match="backed"):
        perform_orphan(coordinator, world.claim, revalidate=revalidate)
    assert world.effects == []
    operation = coordinator.store.inner.operation(ORPHAN_KEY)
    assert operation is not None
    assert operation.status is ST.FAILED
    with pytest.raises(LedgerIntegrityError, match="already failed"):
        perform_orphan(coordinator, world.claim)
    assert world.effects == []


def test_an_unfinished_instance_operation_repeats_only_its_recorded_intent(
    coordinator: Coordinator,
) -> None:
    def unclear() -> Mapping[str, str]:
        raise ProviderError("timeout")

    with pytest.raises(ProviderError):
        perform_orphan(coordinator, unclear)
    assert coordinator.store.inner.operation(ORPHAN_KEY).status is ST.AMBIGUOUS  # type: ignore[union-attr]
    with pytest.raises(LedgerIntegrityError, match="intent differs"):
        perform_orphan(coordinator, World().claim, intent={**ORPHAN_INTENT, "object": "o-2"})
    assert perform_orphan(coordinator, lambda: {"result": "absent"}) == {"result": "absent"}
    assert perform_orphan(coordinator, World().claim) == {"result": "absent"}  # never again


def test_a_superseded_coordinator_neither_records_nor_acts_for_the_instance(
    layout: WorkspaceLayout, path: Path, coordinator: Coordinator
) -> None:
    coordinator.ownership.release()
    other = start(layout, path)
    world = World()
    with pytest.raises(FencingError):
        perform_orphan(coordinator, world.claim)
    assert world.effects == []
    assert other.store.inner.operation(ORPHAN_KEY) is None
    stop(other)


def test_losing_ownership_after_the_instance_intent_stops_the_effect(
    coordinator: Coordinator,
) -> None:
    world = World()
    coordinator.store.after_commit = coordinator.ownership.release
    with pytest.raises(FencingError):
        perform_orphan(coordinator, world.claim)
    assert world.effects == []


def test_the_two_scopes_never_mix(coordinator: Coordinator) -> None:
    with pytest.raises(LedgerIntegrityError, match="use perform_instance"):
        coordinator.perform(K.RELEASE_ORPHAN_CLAIM, World().claim)
    with pytest.raises(LedgerIntegrityError, match="use perform"):
        coordinator.journal.perform_instance(
            K.RELEASE_TASK, World().claim, step="s", intent={}, reason="orphaned-claim",
            revalidate=lambda: None,
        )  # fmt: skip
    perform_orphan(coordinator, lambda: {"result": "absent"})
    with pytest.raises(LedgerIntegrityError, match="no lease settles"):
        coordinator.journal.reconcile(ORPHAN_KEY, ST.SUCCEEDED, lease=coordinator.lease(),
                                      reason="r", observed="claim-absent")  # fmt: skip


@pytest.mark.parametrize("crash_at", [1, 2])
def test_a_crash_in_an_instance_operation_is_repeated_from_its_intent(
    layout: WorkspaceLayout, path: Path, crash_at: int
) -> None:
    c = start(layout, path, crash_at=crash_at)
    world = World()
    with pytest.raises(Crash):
        perform_orphan(c, world.claim)
    assert world.effects == ([] if crash_at == 1 else ["claim"])
    stop(c)
    again = start(layout, path)
    assert perform_orphan(again, world.claim) == {"task": "task-1"}
    operation = again.store.inner.operation(ORPHAN_KEY)
    assert operation is not None
    assert operation.status is ST.SUCCEEDED
    assert dict(operation.intent) == ORPHAN_INTENT
    stop(again)
