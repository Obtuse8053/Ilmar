# SPDX-License-Identifier: Apache-2.0
"""Applying cancellation requests (#16 part 1, ADR-0025 §1a and §4).

* the lease holder alone records ``execution.cancel_requested``, once per
  request, in a fenced commit, with the decision ADR-0025 defines;
* cleanup is rename first, verify after: a concurrent change is preserved in
  ``rejected/`` and reported, never deleted and never applied;
* invalid entries are set aside, crash leftovers settled, I11 holds throughout;
* a stale fence or a released lease records nothing and touches no file.
"""

from __future__ import annotations

import itertools
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import cancel_inbox as fs
from ilmar.core.cancellation import Cancellations
from ilmar.core.journal import Journal
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import ExecutionLease, ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.errors import FencingError, LedgerIntegrityError
from ilmar.domain.event_history import verify_lines
from ilmar.domain.events import Actor, ActorKind, Event, EventType
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Commit, Operation, OperationKind
from ilmar.domain.tasks import TaskRef

from helpers import at

MACHINE = ExecutionStateMachine(max_attempts=3)
FINAL = fs.final_name("e1")


@dataclass
class World:
    layout: WorkspaceLayout
    store: SQLiteExecutionStore
    ownership: Ownership
    journal: Journal
    leases: ExecutionLeases
    lease: ExecutionLease
    windows: dict[str, Callable[[], None]]

    def cancellations(self, journal: Journal | None = None) -> Cancellations:
        tokens = (f"{n:032x}" for n in itertools.count(1))
        return Cancellations(
            store=self.store,
            journal=journal or self.journal,
            layout=self.layout,
            new_token=lambda: next(tokens),
            window=self._window,
        )

    def _window(self, name: str) -> None:
        action = self.windows.pop(name, None)
        if action is not None:
            action()

    @property
    def box(self) -> Path:
        return self.layout.coordinator_root.joinpath(*fs.INBOX_PARTS)

    def inbox(self) -> int:
        return fs.open_inbox(self.layout.coordinator_root, create=True)

    def submit(self, actor: str = "alice") -> fs.CancelRequest:
        fd = self.inbox()
        try:
            submission = fs.submit(fd, "e1", actor, now=at(5))
        finally:
            os.close(fd)
        assert submission.created
        return submission.request

    def execution(self) -> Execution:
        found = self.store.get("e1")
        assert found is not None
        return found

    def requests(self) -> list[Event]:
        return [e for e in self.store.events(execution_id="e1")
                if e.type is EventType.EXECUTION_CANCEL_REQUESTED]  # fmt: skip

    def names(self) -> list[str]:
        return sorted(os.listdir(self.box)) if self.box.exists() else []

    def rejected(self) -> list[str]:
        directory = self.box / fs.REJECTED
        return sorted(os.listdir(directory)) if directory.exists() else []

    def check_i11(self) -> None:
        report = verify_lines(*self.store.history_snapshot())
        assert report.failures == (), report.failures


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    ownership = Ownership.take(layout=layout, store=store, instance_id="inst", now=at(0))
    journal = Journal(store=store, ownership=ownership, clock=lambda: at(10))
    execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(1))
    store.create(execution, transition, fence=ownership.fence)
    leases = ExecutionLeases()
    lease = leases.acquire("e1")
    yield World(layout, store, ownership, journal, leases, lease, {})
    ownership.release()
    store.close()


# -- decisions and recording --------------------------------------------------------------


def test_a_request_is_accepted_recorded_once_and_removed(world: World) -> None:
    request = world.submit()
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert (applied.decision, applied.recorded, applied.reports) == ("accepted", True, ())
    (event,) = world.requests()
    assert event.data == {"request": request.request_id, "decision": "accepted"}
    assert (event.actor.kind.value, event.actor.ref) == ("human", "alice")
    assert event.reason == "cancel-requested"
    assert (event.instance, event.epoch) == ("inst", world.ownership.fence.epoch)
    assert world.names() == []
    assert world.execution().state is ExecutionState.QUEUED  # recording changes no row
    world.check_i11()


def test_applying_again_records_nothing_more(world: World) -> None:
    request = world.submit()
    cancellations = world.cancellations()
    cancellations.apply(world.execution(), world.lease)
    again = cancellations.apply(world.execution(), world.lease, validated=request)
    assert (again.decision, again.recorded) == ("accepted", False)
    assert len(world.requests()) == 1
    assert cancellations.apply(world.execution(), world.lease) == type(again)()


def test_the_request_the_watcher_validated_is_recorded_after_its_name_is_gone(
    world: World,
) -> None:
    request = world.submit()
    os.unlink(world.box / FINAL)
    applied = world.cancellations().apply(world.execution(), world.lease, validated=request)
    assert (applied.decision, applied.recorded) == ("accepted", True)
    assert world.requests()[0].data["request"] == request.request_id


def test_a_request_for_a_finished_execution_is_already_terminal(world: World) -> None:
    updated, step = MACHINE.advance(world.execution(), ExecutionState.FAILED, now=at(2),
                                    reason="r", error="gave up")  # fmt: skip
    failed = Commit(applied=((updated, step),), reasons={"e1": "agent-failed"})
    world.journal.record(failed, lease=world.lease, execution_id="e1")
    world.submit()
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert applied.decision == "already-terminal"
    assert world.names() == []
    world.check_i11()


def test_a_request_after_a_publish_intent_is_too_late(world: World) -> None:
    fence = world.ownership.fence
    publish = Operation.intended(OperationKind.PUBLISH_REVISION, "e1", 1, fence=fence, now=at(3))
    world.journal.record(Commit(operations=(publish,)), lease=world.lease, execution_id="e1")
    world.submit()
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert applied.decision == "too-late"
    assert not world.cancellations().accepted("e1")
    world.check_i11()


def test_a_second_request_after_an_accepted_one_is_a_duplicate(world: World) -> None:
    cancellations = world.cancellations()
    world.submit("alice")
    cancellations.apply(world.execution(), world.lease)
    second = world.submit("bob")
    applied = cancellations.apply(world.execution(), world.lease)
    assert (applied.decision, applied.recorded) == ("duplicate", True)
    assert [e.data["decision"] for e in world.requests()] == ["accepted", "duplicate"]
    assert world.requests()[1].data["request"] == second.request_id
    assert world.names() == []


# -- invalid entries and the watcher's read -----------------------------------------------


def test_an_invalid_entry_is_set_aside_reported_and_never_applied(world: World) -> None:
    fd = world.inbox()
    os.close(fd)
    junk = world.box / FINAL
    junk.write_text('{"schema":"ilmar.cancel-request/1","force":true}')
    junk.chmod(0o600)
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert applied.decision is None
    assert world.requests() == []
    (report,) = applied.reports
    assert "rejected/" in report
    assert world.names() == [fs.REJECTED]
    (kept,) = world.rejected()
    assert (world.box / fs.REJECTED / kept).read_text().endswith('"force":true}')


def test_a_symlink_is_set_aside_without_touching_its_target(world: World, tmp_path: Path) -> None:
    os.close(world.inbox())
    target = tmp_path / "precious.json"
    target.write_text("keep me")
    (world.box / FINAL).symlink_to(target)
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert world.requests() == []
    assert applied.reports
    assert target.read_text() == "keep me"
    (kept,) = world.rejected()
    assert (world.box / fs.REJECTED / kept).is_symlink()


def test_the_watcher_read_never_writes_or_creates(world: World) -> None:
    cancellations = world.cancellations()
    assert cancellations.read("e1").empty
    assert not world.box.parent.exists()  # reading did not create the inbox
    os.close(world.inbox())
    os.mkfifo(world.box / FINAL, 0o600)
    before = world.names()
    assert cancellations.read("e1").problem == "not a regular file"
    assert world.names() == before


# -- races: rename first, verify after ----------------------------------------------------


def _valid_file(world: World, path: Path, request_id: str, actor: str = "mallory") -> None:
    fields = {"schema": fs.SCHEMA, "request": request_id, "execution": "e1", "actor": actor,
              "written_at": "2026-11-09T10:00:00Z"}  # fmt: skip
    path.write_text(fs.encode(fields))
    path.chmod(0o600)


def test_a_request_replaced_after_validation_is_preserved_not_deleted(world: World) -> None:
    first = world.submit()
    other = "q-" + "b" * 32

    def replace() -> None:  # a same-user writer replaces the name after the read
        staged = world.box / "staged"
        _valid_file(world, staged, other)
        os.replace(staged, world.box / FINAL)

    world.windows["recorded"] = replace
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert applied.decision == "accepted"
    assert [e.data["request"] for e in world.requests()] == [first.request_id]
    (report,) = applied.reports
    assert other in report
    assert "resubmit" in report
    (kept,) = world.rejected()
    assert other in (world.box / fs.REJECTED / kept).read_text()  # preserved, not deleted


def test_a_private_name_changed_before_verification_is_preserved(world: World) -> None:
    world.submit()

    def tamper() -> None:
        (applying,), _ = fs.leftovers(fd := world.inbox(), "e1")
        os.close(fd)
        _valid_file(world, world.box / applying, "q-" + "c" * 32)

    world.windows["taken"] = tamper
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert len(applied.reports) == 1
    assert len(world.rejected()) == 1
    assert [n for n in world.names() if n != fs.REJECTED] == []


def test_a_request_removed_after_validation_is_still_recorded(world: World) -> None:
    request = world.submit()
    world.windows["validated"] = lambda: os.unlink(world.box / FINAL)
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert (applied.decision, applied.recorded, applied.reports) == ("accepted", True, ())
    assert world.requests()[0].data["request"] == request.request_id


def test_no_request_is_lost_while_submitters_race_the_applier(world: World) -> None:
    """Every published request ends recorded or preserved on disk, never deleted
    unrecorded, however submissions interleave with apply."""
    published: list[str] = []
    failures: list[str] = []
    stop = threading.Event()

    def submitter() -> None:
        fd = world.inbox()
        try:
            while not stop.is_set():
                try:
                    submission = fs.submit(fd, "e1", "alice", now=at(5))
                except fs.InvalidEntryError as error:  # three removed temporary names
                    failures.append(str(error))
                    continue
                if submission.created:
                    published.append(submission.request.request_id)
        finally:
            os.close(fd)

    thread = threading.Thread(target=submitter)
    thread.start()
    cancellations = world.cancellations()
    try:
        for _ in range(200):
            cancellations.apply(world.execution(), world.lease)
    finally:
        stop.set()
        thread.join()
    cancellations.apply(world.execution(), world.lease)  # settle the last one
    recorded = {str(e.data["request"]) for e in world.requests()}
    on_disk = set()
    for name in world.rejected():
        on_disk.add((world.box / fs.REJECTED / name).read_text())
    for request_id in published:
        assert request_id in recorded or any(request_id in text for text in on_disk), request_id
    assert len(published) > 1
    assert all("kept changing" in failure for failure in failures)  # never anything else
    assert [n for n in world.names() if n != fs.REJECTED] == []
    world.check_i11()


# -- crash leftovers ----------------------------------------------------------------------


def test_a_leftover_private_name_of_a_recorded_request_is_removed(world: World) -> None:
    request = world.submit()
    world.cancellations().apply(world.execution(), world.lease, validated=request)
    # As if the worker crashed after recording and renaming, before unlinking:
    _valid_file(world, world.box / fs.applying_name("e1", "f" * 32), request.request_id)
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert applied.reports == ()
    assert world.names() == []


def test_a_leftover_private_name_of_an_unrecorded_request_is_preserved(world: World) -> None:
    os.close(world.inbox())
    _valid_file(world, world.box / fs.applying_name("e1", "f" * 32), "q-" + "d" * 32)
    applied = world.cancellations().apply(world.execution(), world.lease)
    (report,) = applied.reports
    assert "q-" + "d" * 32 in report
    assert world.requests() == []
    assert len(world.rejected()) == 1


def test_a_submitter_crash_leftover_is_applied_and_both_names_removed(world: World) -> None:
    os.close(world.inbox())
    request_id = "q-" + "e" * 32
    temporary = world.box / fs.temporary_name("e1", request_id)
    _valid_file(world, temporary, request_id, actor="alice")
    os.link(temporary, world.box / FINAL)
    applied = world.cancellations().apply(world.execution(), world.lease)
    assert (applied.decision, applied.reports) == ("accepted", ())
    assert world.names() == []


def test_an_inert_temporary_file_stays_until_a_request_is_accepted(world: World) -> None:
    os.close(world.inbox())
    temporary = world.box / fs.temporary_name("e1", "q-" + "1" * 32)
    temporary.write_text("half")
    temporary.chmod(0o600)
    cancellations = world.cancellations()
    cancellations.apply(world.execution(), world.lease)
    assert world.names() == [temporary.name]  # maybe a live submitter's: left alone
    world.submit()
    cancellations.apply(world.execution(), world.lease)
    cancellations.apply(world.execution(), world.lease)
    assert world.names() == []


# -- fencing ------------------------------------------------------------------------------


def test_a_released_lease_records_nothing_and_touches_no_file(world: World) -> None:
    world.submit()
    before = (world.names(), (world.box / FINAL).read_bytes())
    world.lease.release()
    with pytest.raises(FencingError):
        world.cancellations().apply(world.execution(), world.lease)
    assert world.requests() == []
    assert (world.names(), (world.box / FINAL).read_bytes()) == before


def test_a_superseded_coordinator_records_nothing_and_touches_no_file(world: World) -> None:
    world.submit()
    before = (world.names(), (world.box / FINAL).read_bytes())
    stale = world.cancellations()
    world.ownership.release()
    successor = Ownership.take(layout=world.layout, store=world.store, instance_id="inst",
                               now=at(20))  # fmt: skip
    try:
        with pytest.raises(FencingError):
            stale.apply(world.execution(), world.lease)
        assert world.requests() == []
        assert (world.names(), (world.box / FINAL).read_bytes()) == before
    finally:
        successor.release()


def test_a_cancel_event_for_another_execution_is_refused_by_the_journal(world: World) -> None:
    fence = world.ownership.fence
    other, transition = MACHINE.start(TaskRef("fake", "2"), execution_id="e2", now=at(1))
    world.store.create(other, transition, fence=fence)
    context = world.journal.events.context(fence)
    event = Event.create(
        EventType.EXECUTION_CANCEL_REQUESTED, at=context.at, instance=context.instance,
        epoch=context.epoch, actor=Actor(ActorKind.HUMAN, "alice"), execution="e2",
        reason="cancel-requested", data={"request": "q-" + "a" * 32, "decision": "accepted"},
    )  # fmt: skip
    before = world.store.events()
    with pytest.raises(LedgerIntegrityError, match="cannot change records of e2"):
        world.journal.record(Commit(extra_events=(event,)), lease=world.lease, execution_id="e1")
    assert world.store.events() == before
