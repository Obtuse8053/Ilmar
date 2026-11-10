# SPDX-License-Identifier: Apache-2.0
"""The stray-label sweep in the coordinator (ADR-0027 §1), and ADR-0027's two
clarifications of ADR-0026.

Two passes per removal; never a pick in the pass of a removal; a removal never
implies eligibility; an active execution is protected by the store, never by what
the tracker shows; and every concurrent change, assignment change, delayed read,
forged claim, store loss and restore the ADR names.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import cancel_inbox as fs
from ilmar.core.coordinator import PassReport
from ilmar.core.ownership import Ownership
from ilmar.domain.events import EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit, Operation, OperationKind, OperationStatus
from ilmar.domain.tasks import TaskRef
from tracker_support import check_i11, i13_problems, on_commit, operation_is

from fakes import FakeTaskProvider
from helpers import at

S = ExecutionState
K = OperationKind
ST = OperationStatus
REF = task("1").ref


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


@pytest.fixture
def sqlite() -> Iterator[Callable[[Path], SQLiteExecutionStore]]:
    opened: list[SQLiteExecutionStore] = []

    def open_store(path: Path) -> SQLiteExecutionStore:
        store = SQLiteExecutionStore(path)
        opened.append(store)
        return store

    yield open_store
    for store in opened:
        store.close()


def run(setup: Setup) -> PassReport:
    return setup.coordinator().run_once()


def stray(setup: Setup, ref: TaskRef = REF) -> None:
    """Ilmar's label with nothing behind it: no claim, no human claim, no execution."""
    setup.tasks.tracked(ref).label = True


def sweeps(setup: Setup) -> list[Operation]:
    return [o for o in setup.store.instance_operations() if o.kind is K.CLEAR_STRAY_LABEL]


def clears(setup: Setup) -> list[tuple[str, str, str]]:
    return [w for w in setup.tasks.writes if w[0] == "clear-label"]


def executions(setup: Setup) -> list[str]:
    return [e.id for e in setup.store.list_in_states(set(ExecutionState)) if e.task == REF]


def agent_runs(setup: Setup) -> int:
    return sum(
        1
        for e in setup.store.list_in_states(set(ExecutionState))
        for o in setup.store.operations(e.id)
        if o.kind is K.RUN_AGENT
    )


def our_posts(setup: Setup) -> int:
    return sum(1 for w in setup.tasks.writes if w[0] == "post-claim")


def invariants(setup: Setup, *, completed: bool = True) -> None:
    check_i11(setup.store)
    assert i13_problems(setup.store, setup.tasks, completed=completed) == []


def other_claim(setup: Setup, ref: TaskRef = REF) -> None:
    """Another instance posts a claim comment (its label too, as ADR-0013 does)."""
    claim = setup.tasks.claim_as(ref, "other-instance")
    del claim


def other_comment_only(setup: Setup, ref: TaskRef = REF) -> None:
    """Another instance's claim comment, its label not (yet) added."""
    tracked = setup.tasks.tracked(ref)
    label = tracked.label
    setup.tasks.claim_as(ref, "other-instance")
    tracked.label = label


# -- two passes; never a pick in the pass of the removal --------------------------------------


def test_a_stray_label_is_intended_in_one_pass_and_cleared_in_a_later_one(setup: Setup) -> None:
    setup.tasks.tracked(REF).eligible = True
    stray(setup)
    first = run(setup)
    assert first.summary == "no eligible task"
    (operation,) = sweeps(setup)
    assert operation.status is ST.INTENDED  # the intent only
    assert operation.execution_id is None
    assert dict(operation.intent) == {"task": str(REF)}
    assert clears(setup) == []  # nothing written in the pass of the intent
    run(setup)  # the effect, after a fresh re-read; the task is not picked now
    (operation,) = sweeps(setup)
    assert operation.status is ST.SUCCEEDED
    assert operation.result == {"result": "cleared"}
    assert len(clears(setup)) == 1
    assert executions(setup) == []  # never picked in the pass of the removal
    report = run(setup)  # a later pass: the ordinary path, with fresh reads
    assert report.code == 0, report.summary
    (execution,) = executions(setup)
    assert setup.store.get(execution).state is S.SUCCEEDED  # type: ignore[union-attr]
    events = [e for e in setup.store.events() if e.operation == operation.key]
    assert {e.reason for e in events} == {"stray-label"}
    assert len({e.pass_id for e in events}) >= 2
    invariants(setup)


def test_the_effect_never_runs_in_the_pass_of_its_intent_even_in_recovery(
    setup: Setup,
) -> None:
    stray(setup)
    coordinator = setup.coordinator()
    coordinator.clear_stray_labels([])  # a recovery pass lists and intends...
    coordinator.clear_stray_labels([])  # ...and the same pass, again: never acts
    (operation,) = sweeps(setup)
    assert operation.status is ST.INTENDED
    assert clears(setup) == []
    setup.coordinator().clear_stray_labels([])  # a new pass: acts
    assert sweeps(setup)[0].status is ST.SUCCEEDED


def test_an_unreadable_listing_is_reported_and_decides_nothing(setup: Setup) -> None:
    stray(setup)
    setup.tasks.stray_unreadable = True
    report = run(setup)
    assert "stray labels could not be read" in report.summary
    assert sweeps(setup) == []
    assert setup.tasks.tracked(REF).label


# -- the stranding scenario ------------------------------------------------------------------


def test_an_orphan_beside_a_human_claim_becomes_eligible_once_the_human_lets_go(
    setup: Setup,
) -> None:
    setup.tasks.claim(REF)  # an orphan: Ilmar's claim, with no execution behind it
    setup.tasks.tracked(REF).human = True
    run(setup)  # the orphan release keeps the label beside the human claim (ADR-0026 §5)
    assert setup.tasks.own_claims(REF) == []
    assert setup.tasks.tracked(REF).label
    run(setup)
    run(setup)
    assert sweeps(setup) == []  # never while the human claim exists
    assert setup.tasks.tracked(REF).human
    setup.tasks.tracked(REF).human = False  # the person lets go
    run(setup)  # the intent
    assert setup.tasks.tracked(REF).label
    run(setup)  # the removal; not picked in this pass
    assert not setup.tasks.tracked(REF).label
    assert executions(setup) == []
    run(setup)  # picked through the ordinary path
    (execution,) = executions(setup)
    assert setup.store.get(execution).state is S.SUCCEEDED  # type: ignore[union-attr]
    invariants(setup)


# -- a removal never implies eligibility -----------------------------------------------------


@pytest.mark.parametrize(
    ("hidden", "seen_by"),
    [(3, "list_eligible"), (4, "pass-start"), (5, "claim"), (6, "new-attempt")],
)
def test_a_claim_the_sweep_missed_is_seen_before_any_work(
    setup: Setup, hidden: int, seen_by: str
) -> None:
    """Another instance's claim comment is invisible to the sweep's own two reads
    and to the next ``hidden - 2`` reads of it. The reads, in order: the listing,
    the re-read, the removal pass's ``list_eligible`` (the task is not picked
    there), the next pass's ``list_eligible``, the execution's pass-start
    standing, ``claim``, the new-attempt standing. Whichever fresh read sees it
    first, nothing is run, and only Ilmar's own claim (if any) is released."""
    stray(setup)
    other_comment_only(setup)
    setup.tasks.hidden_others[REF] = hidden
    run(setup)  # the intent (read 1)
    run(setup)  # the removal (read 2): the claim's label goes, its comment stays
    assert clears(setup)
    for _ in range(3):
        run(setup)
    assert agent_runs(setup) == 0
    assert [c.instance for c in setup.tasks.tracked(REF).claims] == ["other-instance"]
    if seen_by == "list_eligible":
        assert executions(setup) == []
        assert our_posts(setup) == 0
        return
    (execution,) = executions(setup)
    assert setup.store.get(execution).state is S.CANCELLED  # type: ignore[union-attr]
    cancelled = [e.reason for e in setup.store.events(execution_id=execution)
                 if e.type is EventType.EXECUTION_TRANSITIONED
                 and e.data.get("to") == "cancelled"]  # fmt: skip
    expected = {"pass-start": "task-claimed-by-other-instance", "claim": "not-claimed-other",
                "new-attempt": "task-claimed-by-other-instance"}  # fmt: skip
    assert cancelled == [expected[seen_by]]
    assert our_posts(setup) == (1 if seen_by == "new-attempt" else 0)
    assert setup.tasks.own_claims(REF) == []  # ours, if any, released
    invariants(setup)


# -- concurrent claims, in every window -------------------------------------------------------

WINDOWS = ["listing", "intent", "pass-boundary", "revalidation", "reread", "removal"]


def arm(setup: Setup, window: str, change: Callable[[], object]) -> Callable[[], None]:
    """Arm ``change`` at ``window``; returns what to call at the pass boundary."""

    def act(*_: object) -> None:
        change()

    if window == "listing":
        setup.tasks.stray_hooks["listed"] = [act]
    elif window == "intent":
        on_commit(setup.store, operation_is(K.CLEAR_STRAY_LABEL, ST.INTENDED), act)
    elif window == "revalidation":
        intents = [0]

        def second_intent(changes: Commit) -> bool:
            if operation_is(K.CLEAR_STRAY_LABEL, ST.INTENDED)(changes):
                intents[0] += 1
            return intents[0] == 2  # the repeated intent, right before revalidation

        on_commit(setup.store, second_intent, act)
    elif window == "reread":
        setup.tasks.stray_hooks["reread"] = [act]
    elif window == "removal":
        setup.tasks.stray_hooks["label"] = [act]
    return act if window == "pass-boundary" else (lambda: None)


def disarm(setup: Setup) -> None:
    setup.tasks.stray_hooks.clear()


@pytest.mark.parametrize("window", WINDOWS)
def test_another_instances_claim_in_any_window_is_never_lost_or_worked_on(
    setup: Setup, window: str
) -> None:
    stray(setup)
    between = arm(setup, window, lambda: other_comment_only(setup))
    run(setup)
    between()
    run(setup)
    disarm(setup)
    tracked = setup.tasks.tracked(REF)
    assert [c.instance for c in tracked.claims] == ["other-instance"]  # never touched
    (operation,) = sweeps(setup)
    # Seen by the fresh re-read in every window but the last; the last is the
    # non-atomic removal itself, where only Ilmar's label goes.
    expected = "cleared" if window == "removal" else "kept"
    assert operation.result == {"result": expected}
    assert tracked.label is (window != "removal")
    for _ in range(2):
        run(setup)
    assert agent_runs(setup) == 0
    assert our_posts(setup) == 0
    invariants(setup)


def test_an_execution_created_between_intent_and_effect_stops_the_removal(setup: Setup) -> None:
    stray(setup)
    run(setup)  # the intent
    execution, created = setup.coordinator()._machine.start(REF, execution_id="late",
                                                             now=setup.clock())  # fmt: skip
    commit(setup, Commit(created=((execution, created),)))
    report = run(setup)
    assert "stray label on" in report.summary
    assert "[ledger-integrity]" in report.summary
    first = sweeps(setup)[0]
    assert first.status is ST.FAILED  # refused by the revalidation: nothing written
    assert clears(setup) == []
    assert setup.tasks.tracked(REF).label


def test_an_active_execution_keeps_its_label_whatever_the_tracker_shows(setup: Setup) -> None:
    """Its own claim comment deleted (or not yet visible): the label is a stray on
    the tracker, but the store backs the task, so nothing is intended or removed."""
    fail = [True]

    def block(ref: TaskRef) -> None:
        if fail and len(setup.tasks.standing_reads) == 4:
            raise_provider()

    setup.tasks.before_standing.append(block)
    run(setup)  # blocked at the freeze: RUNNING_AGENT across passes
    (execution,) = executions(setup)
    tracked = setup.tasks.tracked(REF)
    tracked.claims.clear()  # its comment is gone, or not visible
    assert REF in setup.tasks.stray_labels()
    coordinator = setup.coordinator()
    for _ in range(2):
        setup.coordinator().clear_stray_labels([])
    del coordinator
    assert sweeps(setup) == []
    assert tracked.label
    assert not setup.store.get(execution).state.is_terminal  # type: ignore[union-attr]


def raise_provider() -> None:
    from ilmar.domain.errors import ProviderError

    raise ProviderError("injected: the tracker could not be read")


def test_an_execution_created_after_a_store_restore_stops_the_removal(
    tmp_path: Path, sqlite: Callable[[Path], SQLiteExecutionStore]
) -> None:
    (tmp_path / "s").mkdir()
    db = tmp_path / "executions.sqlite3"
    setup = make_setup(tmp_path / "s", store=sqlite(db))
    stray(setup)
    run(setup)  # the intent
    snapshot = tmp_path / "snapshot.sqlite3"
    for side in ("", "-wal", "-shm"):
        if Path(str(db) + side).exists():
            shutil.copy(Path(str(db) + side), Path(str(snapshot) + side))
    setup.ownership.release()
    setup.store.close()  # type: ignore[attr-defined]
    for side in ("", "-wal", "-shm"):
        Path(str(db) + side).unlink(missing_ok=True)
        if Path(str(snapshot) + side).exists():
            shutil.copy(Path(str(snapshot) + side), Path(str(db) + side))
    restored = sqlite(db)
    setup.store = restored
    setup.components.execution_store = restored
    setup.ownership = Ownership.take(layout=setup.layout, store=restored, instance_id="inst",
                                     now=setup.clock())  # fmt: skip
    execution, created = setup.coordinator()._machine.start(REF, execution_id="after",
                                                             now=setup.clock())  # fmt: skip
    commit(setup, Commit(created=((execution, created),)))
    run(setup)
    assert sweeps(setup)[0].status is ST.FAILED  # created after the intent: refused
    assert clears(setup) == []
    assert setup.tasks.tracked(REF).label


@pytest.mark.parametrize("taken", ["before-intent", "after-intent", "after-effect"])
def test_a_restored_snapshot_ends_in_the_same_tracker_state(
    tmp_path: Path, sqlite: Callable[[Path], SQLiteExecutionStore], taken: str
) -> None:
    (tmp_path / "s").mkdir()
    db = tmp_path / "executions.sqlite3"
    setup = make_setup(tmp_path / "s", store=sqlite(db))
    setup.tasks.tracked(REF).eligible = False  # the sweep alone
    stray(setup)
    snapshot = tmp_path / "snapshot.sqlite3"

    def save() -> None:
        for side in ("", "-wal", "-shm"):
            if Path(str(db) + side).exists():
                shutil.copy(Path(str(db) + side), Path(str(snapshot) + side))

    if taken == "before-intent":
        save()
    run(setup)
    if taken == "after-intent":
        save()
    run(setup)
    if taken == "after-effect":
        save()
    final = repr(setup.tasks.tracked(REF))
    setup.ownership.release()
    setup.store.close()  # type: ignore[attr-defined]
    for side in ("", "-wal", "-shm"):
        Path(str(db) + side).unlink(missing_ok=True)
        if Path(str(snapshot) + side).exists():
            shutil.copy(Path(str(snapshot) + side), Path(str(db) + side))
    restored = sqlite(db)
    setup.store = restored
    setup.components.execution_store = restored
    setup.ownership = Ownership.take(layout=setup.layout, store=restored, instance_id="inst",
                                     now=setup.clock())  # fmt: skip
    for _ in range(3):
        run(setup)
    assert repr(setup.tasks.tracked(REF)) == final
    assert len(clears(setup)) == 1  # removed once; afterwards only ABSENT
    check_i11(restored)


# -- assignment changes ----------------------------------------------------------------------


@pytest.mark.parametrize("skip_assigned", [True, False])
@pytest.mark.parametrize("window", WINDOWS)
def test_an_assignment_in_any_window_is_never_touched(
    tmp_path: Path, window: str, skip_assigned: bool
) -> None:
    setup = make_setup(tmp_path)
    setup.tasks.skip_assigned = skip_assigned
    stray(setup)
    between = arm(setup, window, lambda: setattr(setup.tasks.tracked(REF), "assigned", True))
    run(setup)
    between()
    run(setup)
    disarm(setup)
    tracked = setup.tasks.tracked(REF)
    assert tracked.assigned  # never touched
    (operation,) = sweeps(setup)
    # An assignee that is a claim is seen by the re-read in every window but the
    # removal's own; one that is not a claim never keeps the label.
    kept = skip_assigned and window != "removal"
    assert operation.result == {"result": "kept" if kept else "cleared"}
    assert tracked.label is kept
    for _ in range(2):
        run(setup)
    if skip_assigned:
        assert executions(setup) == []  # the assignee keeps the task out
    invariants(setup)


def test_an_assignee_removed_makes_the_label_stray_only_in_a_later_sweep(setup: Setup) -> None:
    stray(setup)
    setup.tasks.tracked(REF).assigned = True  # a claim while skip_assigned holds
    run(setup)
    run(setup)
    assert sweeps(setup) == []
    setup.tasks.tracked(REF).assigned = False
    run(setup)  # listed now: the intent
    assert setup.tasks.tracked(REF).label
    run(setup)  # cleared one pass later
    assert not setup.tasks.tracked(REF).label
    invariants(setup)


# -- delayed visibility --------------------------------------------------------------------


def test_a_claim_invisible_to_the_listing_but_visible_later_is_kept(setup: Setup) -> None:
    stray(setup)
    other_comment_only(setup)
    setup.tasks.hidden_others[REF] = 1  # the listing alone is blind to it
    run(setup)
    run(setup)
    (operation,) = sweeps(setup)
    assert operation.result == {"result": "kept"}
    events = [e for e in setup.store.events() if e.operation == operation.key]
    assert events[-1].data.get("observed") == "claim-present"
    assert setup.tasks.tracked(REF).label


def test_a_human_claim_label_after_the_listing_is_kept(setup: Setup) -> None:
    stray(setup)
    run(setup)
    setup.tasks.tracked(REF).human = True
    run(setup)
    assert sweeps(setup)[0].result == {"result": "kept"}
    assert setup.tasks.tracked(REF).label
    assert setup.tasks.tracked(REF).human


def test_a_stale_listing_settles_absent(setup: Setup) -> None:
    stray(setup)
    run(setup)
    setup.tasks.tracked(REF).label = False  # a person removed it meanwhile
    run(setup)
    (operation,) = sweeps(setup)
    assert operation.result == {"result": "absent"}
    events = [e for e in setup.store.events() if e.operation == operation.key]
    assert events[-1].data.get("observed") == "claim-label-missing"
    assert clears(setup) == []


# -- forged claims and store loss ------------------------------------------------------------


def test_a_forged_marker_keeps_the_label_until_a_person_removes_it(setup: Setup) -> None:
    stray(setup)
    setup.tasks.forge(REF)
    run(setup)
    run(setup)
    assert sweeps(setup) == []
    assert setup.tasks.tracked(REF).label
    setup.tasks.tracked(REF).claims.clear()  # a person removes the forged comment
    run(setup)
    run(setup)
    assert not setup.tasks.tracked(REF).label


def test_a_fresh_store_over_leftovers_clears_only_what_nothing_claims(tmp_path: Path) -> None:
    tasks = FakeTaskProvider([task("1"), task("2"), task("3")])
    tasks.tracked(task("1").ref).label = True  # a label-only leftover
    tasks.tracked(task("2").ref).label = True
    tasks.tracked(task("2").ref).human = True  # beside a human claim
    tasks.claim(task("3").ref)  # an authentic own orphan
    tasks.tracked(task("3").ref).eligible = False  # not picked again afterwards
    setup = make_setup(tmp_path)
    setup.tasks = tasks
    setup.components.task_provider = tasks
    run(setup)  # orphan released; intents recorded
    run(setup)  # removals
    assert not tasks.tracked(task("1").ref).label
    assert tasks.tracked(task("2").ref).label  # untouched while the human claim exists
    assert tasks.own_claims(task("3").ref) == []
    assert not tasks.tracked(task("3").ref).label
    tasks.tracked(task("2").ref).human = False
    run(setup)
    run(setup)
    assert not tasks.tracked(task("2").ref).label


# -- ADR-0027 §2 and §3: the clarifications --------------------------------------------------


def test_no_standing_read_once_a_cancellation_is_pending(setup: Setup) -> None:
    def request() -> None:
        fd = fs.open_inbox(setup.layout.coordinator_root, create=True)
        try:
            fs.submit(fd, "e1", "alice", now=at(5))
        finally:
            os.close(fd)

    setup.agent.during = request
    run(setup)
    # Reads: the pass start, the new attempt, the agent run. The freeze's safe point
    # applies the request first and never reads the tracker again.
    assert len(setup.tasks.standing_reads) == 3
    assert setup.store.get("e1").state is S.CANCELLED  # type: ignore[union-attr]
    run(setup)
    assert len(setup.tasks.standing_reads) == 3  # terminal: never read again


def test_a_gone_task_is_never_picked_again_even_if_it_comes_back(setup: Setup) -> None:
    def vanish(ref: TaskRef) -> None:
        if len(setup.tasks.standing_reads) == 3:
            setup.tasks.tracked(REF).gone = True

    setup.tasks.before_standing.append(vanish)
    run(setup)
    assert setup.store.get("e1").state is S.CANCELLED  # type: ignore[union-attr]
    setup.tasks.before_standing.clear()
    tracked = setup.tasks.tracked(REF)  # transferred back: a clean, eligible issue
    tracked.gone, tracked.claims, tracked.label = False, [], False
    assert setup.tasks.list_eligible() == (task("1"),)
    for _ in range(2):
        assert run(setup).summary == "no eligible task"
    assert executions(setup) == ["e1"]


def commit(setup: Setup, changes: Commit) -> None:
    events = setup.coordinator()._events
    events.begin_pass()
    setup.store.commit(setup.ownership.fence, changes,
                       context=events.context(setup.ownership.fence))  # fmt: skip
