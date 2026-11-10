# SPDX-License-Identifier: Apache-2.0
"""Tracker reconciliation in the coordinator (ADR-0026 §2 to §5, §7).

Each standing at each safe point; nothing read after a publish intent or for a
terminal execution; human and tracker precedence in both orders; starting a
task again; orphaned claims and every backing rule; the label restore that is
never a decision; and lost-claim confirmation across three ordered passes,
adversarially, with clocks that jump and a restored snapshot.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import cancel_inbox as fs
from ilmar.core.coordinator import PassReport
from ilmar.core.ownership import Ownership
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.errors import FencingError, ProviderError
from ilmar.domain.events import ActorKind, Event, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Operation,
    OperationKind,
    OperationStatus,
    ResourceStatus,
    operation_key,
)
from ilmar.domain.tasks import Claim, HeldClaim, ReleaseResult, RestoreResult, TaskRef
from tracker_support import (
    check_i11,
    i12_problems,
    i13_problems,
    on_commit,
    operation_is,
    tracker_events,
)

from fakes import FakeClaim
from helpers import at

S = ExecutionState
K = OperationKind
ST = OperationStatus
R = ResourceStatus
REF = task("1").ref
LOST = "task-claim-lost"


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


@pytest.fixture
def sqlite() -> Iterator[Callable[[Path], SQLiteExecutionStore]]:
    """Open SQLite stores that are all closed at the end of the test."""
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


def others(setup: Setup) -> list[str]:
    """Executions after the first (ids are not consecutive: tokens share the counter)."""
    return [e.id for e in setup.store.list_in_states(set(ExecutionState)) if e.id != "e1"]


def state(setup: Setup, execution_id: str = "e1") -> ExecutionState:
    execution = setup.store.get(execution_id)
    assert execution is not None
    return execution.state


def into(setup: Setup, target: ExecutionState, execution_id: str = "e1") -> list[str | None]:
    return [e.reason for e in setup.store.events(execution_id=execution_id)
            if e.type is EventType.EXECUTION_TRANSITIONED
            and e.data.get("to") == target.value]  # fmt: skip


def decisions(setup: Setup, execution_id: str = "e1") -> list[Event]:
    return [e for e in tracker_events(setup.store, execution_id)
            if e.type is EventType.EXECUTION_CANCEL_REQUESTED]  # fmt: skip


def lost(setup: Setup, execution_id: str = "e1") -> list[tuple[str, str | None]]:
    return [(str(e.data["state"]), e.data.get("observed"))
            for e in tracker_events(setup.store, execution_id)
            if e.type is EventType.EXECUTION_CLAIM_LOST]  # fmt: skip


def invariants(setup: Setup, *, completed: bool = True) -> None:
    check_i11(setup.store)
    for execution in setup.store.list_in_states(set(ExecutionState)):
        assert i12_problems(setup.store, execution.id) == []
    assert i13_problems(setup.store, setup.tasks, completed=completed) == []


def at_read(setup: Setup, number: int, action: Callable[[], object]) -> None:
    """Run ``action`` right before the ``number``-th standing read (1-based)."""
    count = [0]

    def hook(ref: TaskRef) -> None:
        count[0] += 1
        if count[0] == number:
            action()

    setup.tasks.before_standing.append(hook)


def fail_reads(setup: Setup, numbers: set[int]) -> list[int]:
    """Make the given standing reads (1-based, counted from now) fail: a 403."""
    count = [0]

    def hook(ref: TaskRef) -> None:
        count[0] += 1
        if count[0] in numbers:
            raise ProviderError("injected: the tracker could not be read")

    setup.tasks.before_standing.append(hook)
    return count


def lose(setup: Setup, failing: set[int]) -> Callable[[], None]:
    """During the agent run, a person deletes Ilmar's claim; from then on the given
    standing reads fail, so that passes end where the test needs them to."""

    def act() -> None:
        drop_own_claim(setup)
        fail_reads(setup, failing)

    return act


def drop_own_claim(setup: Setup, ref: TaskRef = REF) -> None:
    tracked = setup.tasks.tracked(ref)
    tracked.claims = [c for c in tracked.claims if c not in setup.tasks.own_claims(ref)]


def change(setup: Setup, what: str, ref: TaskRef = REF) -> None:
    tracked = setup.tasks.tracked(ref)
    if what == "close":
        tracked.open = False
    elif what == "unlabel":
        tracked.eligible = False
    elif what == "exclude":
        tracked.excluded = True
    elif what == "human":
        tracked.human = True
    elif what == "other-instance":
        setup.tasks.claim_as(ref, "someone-else")
    elif what == "gone":
        tracked.gone = True
    else:
        raise AssertionError(what)


REASONS = {
    "close": "task-closed",
    "unlabel": "task-unlabelled",
    "exclude": "task-excluded",
    "human": "task-claimed-by-human",
    "other-instance": "task-claimed-by-other-instance",
    "gone": "task-gone",
}
# The standing reads of a fresh execution in its first pass, in order (ADR-0026 §2).
POINTS = {
    "pass-start": 1,
    "new-attempt": 2,
    "agent-run": 3,
    "freeze": 4,
    "verification": 5,
    "first-push": 6,
}


def test_a_fresh_execution_reads_at_exactly_the_safe_points(setup: Setup) -> None:
    report = run(setup)
    assert report.code == 0, report.summary
    assert setup.tasks.standing_reads == [REF] * len(POINTS)
    assert tracker_events(setup.store) == []
    invariants(setup)


def test_the_execution_record_is_written_before_the_claim(setup: Setup) -> None:
    """#15: a claim is never posted before its record exists (ADR-0021)."""
    seen: list[object] = []
    original = setup.tasks.claim

    def claim(ref: TaskRef) -> Claim:
        seen.append(setup.store.active_for_task(ref))
        return original(ref)

    setup.tasks.claim = claim  # type: ignore[method-assign]
    run(setup)
    assert seen
    assert seen[0] is not None


# -- each standing at each safe point (§2, §3) ---------------------------------------------


@pytest.mark.parametrize("point", list(POINTS))
@pytest.mark.parametrize("what", sorted(REASONS))
def test_each_standing_decides_at_each_safe_point(setup: Setup, what: str, point: str) -> None:
    at_read(setup, POINTS[point], lambda: change(setup, what))
    report = run(setup)
    reason = REASONS[what]
    assert state(setup) is S.CANCELLED, report.summary
    assert into(setup, S.CANCELLED) == [reason]
    (decision,) = decisions(setup)
    assert decision.actor.kind is ActorKind.COORDINATOR
    assert decision.reason == reason
    assert dict(decision.data) == {"decision": "accepted", "observed": reason}
    assert decision.pass_id is not None
    assert setup.scm.pushes == []
    assert setup.runtime._containers == {}
    claimed = point != "pass-start"
    release = setup.store.operation(operation_key(K.RELEASE_TASK, "e1", 1, "cancelled"))
    assert (release is not None) is claimed
    if claimed:
        assert release is not None
        assert release.status is ST.SUCCEEDED
    if what != "gone":  # a vanished task has nothing left to release
        assert setup.tasks.own_claims(REF) == []  # only our claim is released
    tracked = setup.tasks.tracked(REF)
    if what == "human":
        assert tracked.human  # a person's claim is never touched
    if what == "other-instance":
        assert [c.instance for c in tracked.claims] == ["someone-else"]
    notes = setup.tasks.updates(REF)
    assert len(notes) == (1 if claimed and what != "gone" else 0)
    if notes:
        assert notes[0].startswith("Ilmar stopped working on this task: ")
    assert f"[{reason}]" in report.summary
    invariants(setup)


def test_an_uncertain_read_decides_nothing_and_blocks_the_pass(setup: Setup) -> None:
    fail_reads(setup, {4})  # the freeze's read
    report = run(setup)
    assert "[transient-provider-error]" in report.summary
    assert state(setup) is S.RUNNING_AGENT
    assert tracker_events(setup.store) == []
    assert not any(o.kind is K.FREEZE_CANDIDATE for o in setup.store.operations("e1"))
    assert run(setup).code == 0  # the next pass reads again and goes on
    assert state(setup) is S.SUCCEEDED
    invariants(setup)


# -- never read after a publish intent, nor for a terminal execution (§2, §4) -------------------


def test_nothing_is_read_or_decided_after_a_publish_intent(setup: Setup) -> None:
    on_commit(setup.store, operation_is(K.PUBLISH_REVISION, ST.INTENDED),
              lambda: change(setup, "close"))  # fmt: skip
    report = run(setup)
    assert report.code == 0, report.summary
    assert state(setup) is S.SUCCEEDED
    assert setup.tasks.standing_reads == [REF] * len(POINTS)  # none after the intent
    assert tracker_events(setup.store) == []
    assert setup.tasks.own_claims(REF)  # a closed issue with an open change request
    run(setup)  # terminal: never read again; the claim is kept for the follow-up
    assert setup.tasks.standing_reads == [REF] * len(POINTS)
    assert setup.tasks.own_claims(REF)
    invariants(setup)


def test_a_failure_first_stays_a_failure(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.FAILED, {})]
    run(setup)
    assert state(setup) is S.FAILED
    change(setup, "close")
    run(setup)
    assert state(setup) is S.FAILED
    assert decisions(setup) == []
    invariants(setup)


# -- human and tracker precedence (§4) ------------------------------------------------------


def submit(setup: Setup, execution_id: str = "e1") -> None:
    fd = fs.open_inbox(setup.layout.coordinator_root, create=True)
    try:
        fs.submit(fd, execution_id, "alice", now=at(5))
    finally:
        os.close(fd)


def test_a_person_first_wins_over_the_tracker(setup: Setup) -> None:
    def both() -> None:
        submit(setup)
        change(setup, "close")

    setup.agent.during = both
    run(setup)
    assert state(setup) is S.CANCELLED
    assert into(setup, S.CANCELLED) == ["cancel-requested"]
    assert [(e.actor.kind, e.data["decision"]) for e in decisions(setup)] == [
        (ActorKind.HUMAN, "accepted")
    ]
    invariants(setup)


def test_the_tracker_first_wins_over_a_person(setup: Setup) -> None:
    at_read(setup, POINTS["freeze"], lambda: change(setup, "close"))
    original = setup.tasks.release

    def release_then_request(ref: TaskRef) -> None:
        submit(setup)  # a person asks while the ending runs
        original(ref)

    setup.tasks.release = release_then_request  # type: ignore[method-assign]
    run(setup)
    run(setup)  # a request that arrives too late is recorded on the next pass
    assert state(setup) is S.CANCELLED
    assert into(setup, S.CANCELLED) == ["task-closed"]
    assert [(e.actor.kind, e.data["decision"]) for e in decisions(setup)] == [
        (ActorKind.COORDINATOR, "accepted"),
        (ActorKind.HUMAN, "already-terminal"),
    ]
    invariants(setup)


# -- starting again (§3) --------------------------------------------------------------------


@pytest.mark.parametrize(("what", "undo"), [("unlabel", "eligible"), ("human", "human"),
                                            ("close", "open")])  # fmt: skip
def test_a_task_the_tracker_made_ineligible_is_picked_again_as_a_new_execution(
    setup: Setup, what: str, undo: str
) -> None:
    at_read(setup, POINTS["agent-run"], lambda: change(setup, what))
    run(setup)
    assert state(setup) is S.CANCELLED
    tracked = setup.tasks.tracked(REF)
    assert run(setup).summary == "no eligible task"  # still ineligible: nothing
    setattr(tracked, undo, undo != "human")
    report = run(setup)
    assert report.code == 0, report.summary
    (new,) = others(setup)
    assert state(setup, new) is S.SUCCEEDED
    assert state(setup) is S.CANCELLED  # never resumed
    invariants(setup)


@pytest.mark.parametrize("cause", ["person", "lost-claim", "gone"])
def test_a_task_is_never_picked_again_after_a_person_a_lost_claim_or_its_absence(
    setup: Setup, cause: str
) -> None:
    if cause == "person":
        setup.agent.during = lambda: submit(setup)
        run(setup)
    elif cause == "gone":
        at_read(setup, POINTS["agent-run"], lambda: change(setup, "gone"))
        run(setup)
        tracked = setup.tasks.tracked(REF)  # it comes back, as a new issue would
        tracked.gone, tracked.claims, tracked.label = False, [], False
    else:
        confirm_lost_claim(setup)
        assert not setup.tasks.tracked(REF).label  # Ilmar removed its leftover label
    assert state(setup) is S.CANCELLED
    assert setup.tasks.list_eligible() == (task("1"),)  # eligible on the tracker again
    assert run(setup).summary == "no eligible task"
    assert others(setup) == []
    invariants(setup)


def test_a_task_whose_claim_release_is_unfinished_is_never_picked(setup: Setup) -> None:
    at_read(setup, POINTS["agent-run"], lambda: change(setup, "unlabel"))
    original = setup.tasks.release
    failing = [True]

    def release(ref: TaskRef) -> None:
        if failing:
            raise ProviderError("the tracker is down")
        original(ref)

    setup.tasks.release = release  # type: ignore[method-assign]
    run(setup)
    assert state(setup) is S.CANCELLED
    setup.tasks.tracked(REF).eligible = True
    report = run(setup)  # the release fails again: never picked meanwhile
    assert others(setup) == [], report.summary
    failing.clear()
    run(setup)  # the release completes; the task is picked in the same pass
    assert len(others(setup)) == 1
    invariants(setup)


# -- the label restore: never a claim, never a decision (§5) ---------------------------------


def test_a_missing_label_is_restored_beside_the_authentic_claim(setup: Setup) -> None:
    def drop_label() -> None:
        setup.tasks.tracked(REF).label = False

    setup.agent.during = drop_label
    run(setup)
    assert state(setup) is S.SUCCEEDED
    assert setup.tasks.tracked(REF).label
    (restore,) = [o for o in setup.store.operations("e1") if o.kind is K.RESTORE_CLAIM_LABEL]
    assert restore.status is ST.SUCCEEDED
    (own,) = setup.tasks.own_claims(REF)
    assert dict(restore.intent) == {"task": str(REF), "claim": own.token, "object": own.object_id}
    labels = [w for w in setup.tasks.writes if w[0] == "label"]
    assert len(labels) == 1  # the restore's: its only write
    assert tracker_events(setup.store) == []
    invariants(setup)


def test_a_claim_deleted_before_the_restore_is_absent_and_never_reclaimed(setup: Setup) -> None:
    setup.agent.during = lambda: setattr(setup.tasks.tracked(REF), "label", False)
    setup.tasks.hooks["reread"] = [lambda claim: drop_own_claim(setup)]
    run(setup)
    (restore,) = [o for o in setup.store.operations("e1") if o.kind is K.RESTORE_CLAIM_LABEL]
    assert restore.status is ST.SUCCEEDED
    assert restore.result == {"result": "absent"}
    posts = [w for w in setup.tasks.writes if w[0] == "post-claim"]
    assert len(posts) == 1  # the original claim only: the human's deletion stands
    assert decisions(setup) == []  # an absent restore is never a decision
    assert not any(e.data.get("observed") == "task-claimed-by-other-instance"
                   for e in setup.store.events(execution_id="e1"))  # fmt: skip
    events = [e for e in setup.store.events(execution_id="e1") if e.operation == restore.key]
    assert events[-1].data.get("observed") == "claim-absent"
    invariants(setup)


@pytest.mark.parametrize("how", ["token", "instance", "forged"])
def test_a_changed_claim_gets_no_label(setup: Setup, how: str) -> None:
    setup.agent.during = lambda: setattr(setup.tasks.tracked(REF), "label", False)

    def tamper(claim: HeldClaim) -> None:
        (own,) = setup.tasks.own_claims(REF)
        if how == "token":
            own.token = "t" + "9" * 31
        elif how == "instance":
            own.instance = "someone-else"
        else:
            own.author = "mallory"

    setup.tasks.hooks["reread"] = [tamper]
    original = setup.tasks.restore_label
    seen: list[bool] = []

    def restore(claim: HeldClaim) -> RestoreResult:
        result = original(claim)
        seen.append(setup.tasks.tracked(REF).label)
        setup.tasks.hooks.clear()
        return result

    setup.tasks.restore_label = restore  # type: ignore[method-assign]
    run(setup)
    restore_ = next(o for o in setup.store.operations("e1") if o.kind is K.RESTORE_CLAIM_LABEL)
    assert restore_.status is ST.FAILED
    assert seen[0] is False  # no label beside a changed object
    failed = [e for e in setup.store.events(execution_id="e1") if e.operation == restore_.key
              and e.type is EventType.OPERATION_FAILED]  # fmt: skip
    assert failed[0].reason == "claim-changed"
    invariants(setup, completed=False)


def test_an_interrupted_restore_then_a_deleted_claim_is_confirmed_only_by_two_passes(
    setup: Setup,
) -> None:
    """The regression for the review of cc295d7: an interrupted restore that had
    already re-added the label; the claim comment then deleted by a person; the
    restore performed again settles ABSENT, with no cancellation in that pass.
    The lost claim is confirmed only under §3a, by two later passes."""
    setup.agent.during = lambda: setattr(setup.tasks.tracked(REF), "label", False)
    original = setup.tasks.restore_label

    def restore_then_die(claim: HeldClaim) -> RestoreResult:
        original(claim)  # the label is back...
        setup.tasks.restore_label = original  # type: ignore[method-assign]
        raise ProviderError("...but the outcome was lost")

    setup.tasks.restore_label = restore_then_die  # type: ignore[method-assign]
    report = run(setup)  # pass 1 (P0): the restore is ambiguous; blocked
    assert "ProviderError" in report.summary or "transient" in report.summary
    drop_own_claim(setup)  # a person deletes Ilmar's claim; the label stays
    count = fail_reads(setup, {2, 4})  # end passes 2 and 3 after their first read
    run(setup)  # pass 2: the restore settles ABSENT; the first observation
    (restore,) = [o for o in setup.store.operations("e1") if o.kind is K.RESTORE_CLAIM_LABEL]
    assert restore.status is ST.SUCCEEDED
    assert restore.result == {"result": "absent"}
    assert decisions(setup) == []
    assert lost(setup) == [("observed", LOST)]
    assert not [w for w in setup.tasks.writes if w[0] == "post-claim"][1:]
    run(setup)  # pass 3: confirmed
    assert count[0] >= 3
    assert [e.reason for e in decisions(setup)] == [LOST]
    assert state(setup) is S.CANCELLED
    assert into(setup, S.CANCELLED) == [LOST]
    invariants(setup)


# -- confirming a lost claim (§3a) ------------------------------------------------------------


def confirm_lost_claim(setup: Setup) -> list[PassReport]:
    """The claim is deleted during the agent run in pass 1 (P0). Every other read
    from then on fails, so each pass makes one successful read: P1 observes and
    P2 confirms."""
    setup.agent.during = lose(setup, {1, 3, 5, 7})
    return [run(setup) for _ in range(3)]


def test_a_single_observation_never_cancels(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3})
    run(setup)  # P0: the reads after the deletion are in the claim's own pass
    assert lost(setup) == []
    run(setup)  # P1: one observation, then a failed read ends the pass
    assert lost(setup) == [("observed", LOST)]
    assert decisions(setup) == []
    assert state(setup) is S.RUNNING_AGENT
    invariants(setup, completed=False)


def test_a_second_read_in_the_same_pass_records_nothing(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1})
    commits: list[Commit] = []
    original = setup.store.commit

    def capture(fence: object, changes: Commit, *, context: object = None) -> None:
        original(fence, changes, context=context)  # type: ignore[arg-type]
        commits.append(changes)

    setup.store.commit = capture  # type: ignore[method-assign]
    run(setup)  # P0, blocked at the freeze
    report = run(setup)  # P1: observes at the start, then reads again at the freeze...
    assert decisions(setup) == []  # ...which records nothing more
    assert state(setup) is S.SUCCEEDED, report.summary  # the claim lost slowly: published
    # The only clearing is the terminal transition's own (§3a, "Reset").
    assert lost(setup) == [("observed", LOST), ("cleared", None)]
    (closing,) = [c for c in commits if any(
        e.type is EventType.EXECUTION_CLAIM_LOST and e.data.get("state") == "cleared"
        for e in c.extra_events)]  # fmt: skip
    assert [t.to_state for _, t in closing.applied] == [S.SUCCEEDED]  # in the same commit
    invariants(setup, completed=False)


def test_reads_in_the_claim_writes_own_pass_are_never_counted(setup: Setup) -> None:
    setup.agent.during = lambda: drop_own_claim(setup)
    report = run(setup)  # freeze, verification and push read CLAIM_LOST in P0
    assert report.code == 0, report.summary
    assert state(setup) is S.SUCCEEDED
    assert lost(setup) == []
    assert decisions(setup) == []


def test_a_lost_claim_is_confirmed_in_three_ordered_passes(setup: Setup) -> None:
    reports = confirm_lost_claim(setup)
    assert state(setup) is S.CANCELLED, [r.summary for r in reports]
    assert into(setup, S.CANCELLED) == [LOST]
    events = tracker_events(setup.store)
    assert [(e.type.value, e.data.get("state")) for e in events] == [
        ("execution.claim_lost", "observed"),
        ("execution.claim_lost", "cleared"),
        ("execution.cancel_requested", None),
    ]
    observed, cleared, decided = events
    assert decided.data == {"decision": "accepted", "observed": LOST}
    assert (cleared.seq or 0) + 1 == decided.seq  # one commit
    assert cleared.pass_id == decided.pass_id != observed.pass_id
    claim = next(o for o in setup.store.operations("e1") if o.kind is K.CLAIM_TASK)
    assert observed.data["claim"] == claim.result["claim_id"]
    assert not any(o.kind is K.FREEZE_CANDIDATE for o in setup.store.operations("e1"))
    invariants(setup)


def test_a_reappearing_claim_resets_and_a_later_loss_needs_two_observations(
    setup: Setup,
) -> None:
    saved: list[FakeClaim] = []

    def delete() -> None:
        saved.extend(setup.tasks.own_claims(REF))
        drop_own_claim(setup)
        fail_reads(setup, {1, 3, 5, 7, 9})

    setup.agent.during = delete
    run(setup)  # P0
    run(setup)  # P1: observed
    setup.tasks.tracked(REF).claims.extend(saved)  # the claim reappears
    run(setup)  # a HELD read: cleared
    assert lost(setup) == [("observed", LOST), ("cleared", "claim-present")]
    drop_own_claim(setup)
    run(setup)  # observed again
    assert decisions(setup) == []
    run(setup)  # confirmed only now
    assert [e.reason for e in decisions(setup)] == [LOST]
    assert lost(setup) == [
        ("observed", LOST),
        ("cleared", "claim-present"),
        ("observed", LOST),
        ("cleared", LOST),
    ]
    invariants(setup)


def test_an_observation_of_another_claim_never_confirms(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3, 5})
    run(setup)  # P0
    commit_extra(setup, observation(setup, claim="t-not-ours"))
    run(setup)  # CLAIM_LOST: the foreign observation is closed; ours opens
    assert lost(setup) == [("observed", LOST), ("cleared", None), ("observed", LOST)]
    assert decisions(setup) == []
    run(setup)
    assert [e.reason for e in decisions(setup)] == [LOST]
    invariants(setup)


def test_a_claim_write_after_an_observation_clears_it(setup: Setup) -> None:
    """Any commit of the claim operation resets (§3a): an execution whose claim was
    interrupted, with an observation somehow open, clears it before the repeat."""
    fence = setup.ownership.fence
    execution, created = setup.coordinator()._machine.start(REF, execution_id="e1", now=at(1))
    commit(setup, Commit(created=((execution, created),)))
    claim = Operation.intended(K.CLAIM_TASK, "e1", 1, fence=fence, now=at(2))
    commit(setup, Commit(operations=(claim,)))  # crashed after the intent
    commit_extra(setup, observation(setup, claim="t-old"))
    run(setup)
    events = setup.store.events(execution_id="e1")
    cleared = next(e for e in events if e.type is EventType.EXECUTION_CLAIM_LOST
                   and e.data["state"] == "cleared")  # fmt: skip
    intents = [e for e in events if e.operation == claim.key
               and e.type is EventType.OPERATION_INTENDED]  # fmt: skip
    assert len(intents) == 2  # the interrupted intent, and its repeat
    repeat = intents[-1]
    assert (cleared.seq or 0) < (repeat.seq or 0)
    assert decisions(setup) == []
    assert state(setup) is S.SUCCEEDED
    invariants(setup)


def test_a_lost_claim_is_ignored_while_the_claim_is_unfinished(setup: Setup) -> None:
    fence = setup.ownership.fence
    execution, created = setup.coordinator()._machine.start(REF, execution_id="e1", now=at(1))
    commit(setup, Commit(created=((execution, created),)))
    commit(setup, Commit(operations=(
        Operation.intended(K.CLAIM_TASK, "e1", 1, fence=fence, now=at(2)),)))  # fmt: skip
    fail_reads(setup, {2})  # the read after the pass start fails: one read only
    run(setup)
    assert lost(setup) == []


def test_an_ineligible_read_with_an_open_observation_decides_at_once(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3})
    run(setup)
    run(setup)  # P1 observed
    change(setup, "close")
    run(setup)
    assert [e.reason for e in decisions(setup)] == ["task-closed"]
    assert lost(setup) == [("observed", LOST), ("cleared", "task-closed")]
    assert into(setup, S.CANCELLED) == ["task-closed"]
    invariants(setup)


# -- read-after-write lag ------------------------------------------------------------------


def test_a_claim_invisible_for_its_whole_pass_never_cancels(setup: Setup) -> None:
    setup.tasks.lagging[REF] = 100
    original = setup.tasks.claim

    def claim(ref: TaskRef) -> Claim:
        result = original(ref)
        setup.tasks.lagging[ref] = 100  # invisible for every read of this pass
        return result

    setup.tasks.claim = claim  # type: ignore[method-assign]
    report = run(setup)
    assert report.code == 0, report.summary
    assert lost(setup) == []
    assert decisions(setup) == []


def test_a_claim_invisible_for_one_more_pass_resets_and_never_cancels(setup: Setup) -> None:
    original = setup.tasks.claim

    def claim(ref: TaskRef) -> Claim:
        result = original(ref)
        setup.tasks.lagging[ref] = 3  # the rest of P0 and the start of P1
        fail_reads(setup, {3, 5})  # P0 ends at its 3rd read after the claim; P1 at 5th
        return result

    setup.tasks.claim = claim  # type: ignore[method-assign]
    run(setup)
    run(setup)  # P1: observed (still stale)
    assert lost(setup) == [("observed", LOST)]
    report = run(setup)  # visible again: cleared, and it publishes
    assert lost(setup) == [("observed", LOST), ("cleared", "claim-present")]
    assert decisions(setup) == []
    assert state(setup) is S.SUCCEEDED, report.summary
    invariants(setup)


# -- fencing, epochs, time and snapshots ------------------------------------------------------


def test_a_fenced_out_coordinator_records_nothing(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3, 5})
    run(setup)  # P0
    stale = setup.coordinator()
    setup.ownership.release()
    Ownership.take(layout=setup.layout, store=setup.store, instance_id="inst",
                   now=setup.clock())  # fmt: skip
    before = setup.store.events()
    with pytest.raises(FencingError):
        stale.run_once()
    assert setup.store.events() == before


def test_an_observation_from_an_earlier_epoch_is_revalidated(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3, 5})
    run(setup)  # P0
    run(setup)  # P1: observed in epoch 1
    setup.ownership.release()
    setup.ownership = Ownership.take(layout=setup.layout, store=setup.store,
                                     instance_id="inst", now=setup.clock())  # fmt: skip
    run(setup)  # epoch 2: a fresh CLAIM_LOST read confirms under rules 3 to 5
    (decided,) = decisions(setup)
    assert decided.epoch == setup.ownership.fence.epoch
    (observed,) = [e for e in tracker_events(setup.store) if e.data.get("state") == "observed"]
    assert observed.epoch < decided.epoch
    invariants(setup)


def test_an_observation_from_an_earlier_epoch_alone_is_never_enough(setup: Setup) -> None:
    setup.agent.during = lose(setup, {1, 3, 5})
    run(setup)
    run(setup)  # P1: observed in epoch 1
    setup.tasks.tracked(REF).claims.append(_first_claim(setup))  # the claim is back
    setup.ownership.release()
    setup.ownership = Ownership.take(layout=setup.layout, store=setup.store,
                                     instance_id="inst", now=setup.clock())  # fmt: skip
    run(setup)  # the fresh read sees the claim: cleared, never confirmed
    assert decisions(setup) == []
    assert lost(setup)[-1] == ("cleared", "claim-present")


def _first_claim(setup: Setup) -> FakeClaim:
    claim = next(o for o in setup.store.operations("e1") if o.kind is K.CLAIM_TASK)
    return FakeClaim("o-x", "ilmar", claim.result["claim_id"], "ilmar-bot")


def decision_trace(setup: Setup) -> list[tuple[str, str | None, int]]:
    """Every tracker event as (type, state, index of its pass): no ids, no times."""
    passes: list[str | None] = []
    trace = []
    for event in tracker_events(setup.store):
        if event.pass_id not in passes:
            passes.append(event.pass_id)
        trace.append((event.type.value, event.data.get("state"), passes.index(event.pass_id)))
    return trace


@pytest.mark.parametrize("clock", ["steady", "frozen", "back-hours", "forward-days", "jitter"])
def test_the_wall_clock_never_changes_a_decision(tmp_path: Path, clock: str) -> None:
    for name in ("reference", "clock"):
        (tmp_path / name).mkdir()
    reference = make_setup(tmp_path / "reference")
    confirm_lost_claim(reference)
    expected = decision_trace(reference)
    setup = make_setup(tmp_path / "clock")
    base = setup.clock.now
    calls = [0]

    def odd() -> object:
        calls[0] += 1
        if clock == "frozen":
            return base
        if clock == "back-hours":
            return base - timedelta(hours=calls[0] % 7)
        if clock == "forward-days":
            return base + timedelta(days=calls[0] * 3)
        if clock == "jitter":
            return base + timedelta(seconds=(-1) ** calls[0] * calls[0])
        base_now = setup.clock.now + timedelta(seconds=1)
        setup.clock.now = base_now
        return base_now

    setup.clock.__class__ = type("Odd", (setup.clock.__class__,), {"__call__": lambda self: odd()})
    confirm_lost_claim(setup)
    assert decision_trace(setup) == expected
    assert state(setup) is S.CANCELLED
    assert into(setup, S.CANCELLED) == [LOST]


def test_a_restored_snapshot_does_not_change_the_outcome(
    tmp_path: Path, sqlite: Callable[[Path], SQLiteExecutionStore]
) -> None:
    (tmp_path / "a").mkdir()
    db = tmp_path / "executions.sqlite3"
    setup = make_setup(tmp_path / "a", store=sqlite(db))
    setup.agent.during = lose(setup, {1, 3, 5, 7})
    run(setup)  # P0
    run(setup)  # P1: observed
    snapshot = tmp_path / "snapshot.sqlite3"
    for side in ("", "-wal", "-shm"):  # the file as a backup tool would copy it
        if Path(str(db) + side).exists():
            shutil.copy(Path(str(db) + side), Path(str(snapshot) + side))
    run(setup)  # P2: confirmed and cancelled
    assert state(setup) is S.CANCELLED
    # Restore the snapshot taken after P1 and run again on it.
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
    run(setup)  # the scripted tracker fails this pass's only read: nothing decided
    assert decisions(setup) == []
    run(setup)  # a fresh CLAIM_LOST read: the restored observation confirms
    assert state(setup) is S.CANCELLED
    assert into(setup, S.CANCELLED) == [LOST]
    assert [e.reason for e in decisions(setup)] == [LOST]
    invariants(setup)


# -- helpers that write events directly (synthetic durable states) ---------------------------


def observation(setup: Setup, *, claim: str) -> Event:
    from ilmar.core import tracker

    events = setup.coordinator()._events
    events.begin_pass()
    context = events.context(setup.ownership.fence)
    return tracker.claim_lost(context, "e1", state="observed", claim=claim, observed=LOST)


def commit_extra(setup: Setup, event: Event) -> None:
    commit(setup, Commit(extra_events=(event,)))


def commit(setup: Setup, changes: Commit) -> None:
    """A write as an earlier pass of this coordinator made it: fenced, with a pass."""
    events = setup.coordinator()._events
    events.begin_pass()
    setup.store.commit(setup.ownership.fence, changes,
                       context=events.context(setup.ownership.fence))  # fmt: skip


# -- orphaned claims (§5) -----------------------------------------------------------------------


def orphan(setup: Setup, ref: TaskRef = REF) -> FakeClaim:
    """A claim of this instance that no execution backs: lost or replaced state."""
    setup.tasks.claim(ref)
    (claim,) = setup.tasks.own_claims(ref)
    return claim


def test_an_orphaned_claim_is_released_before_new_work(setup: Setup) -> None:
    old = orphan(setup)
    report = run(setup)
    assert "released 1 orphaned claim(s)" in report.summary
    (operation,) = setup.store.instance_operations()
    assert operation.status is ST.SUCCEEDED
    assert operation.execution_id is None
    assert dict(operation.intent) == {"task": str(REF), "claim": old.token,
                                      "object": old.object_id}  # fmt: skip
    deletes = [w for w in setup.tasks.writes if w[0] == "delete-claim"]
    posts = [w for w in setup.tasks.writes if w[0] == "post-claim"]
    assert deletes[0][2] == old.object_id
    assert setup.tasks.writes.index(deletes[0]) < setup.tasks.writes.index(posts[-1])
    assert state(setup) is S.SUCCEEDED  # then the task was picked and published
    invariants(setup)


def test_a_claim_of_a_lost_state_directory_is_released_before_the_first_pick(
    tmp_path: Path,
) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = make_setup(tmp_path / "first")
    first.tasks.add(task("2"))
    run(first)  # publishes task 1; the claim stays while its change request is open
    lost_state = make_setup(tmp_path / "second", tasks=())
    lost_state.tasks = first.tasks  # the same tracker, an empty store
    lost_state.components.task_provider = first.tasks
    report = run(lost_state)
    assert "released 1 orphaned claim(s)" in report.summary
    released = [w for w in first.tasks.writes if w[0] == "delete-claim"]
    assert released[0][1] == str(REF)


def test_others_claims_are_never_touched(setup: Setup) -> None:
    setup.tasks.claim_as(REF, "someone-else")
    setup.tasks.tracked(REF).human = True
    setup.tasks.forge(REF)
    before = repr(setup.tasks.tracked(REF))
    run(setup)
    assert repr(setup.tasks.tracked(REF)) == before
    assert setup.store.instance_operations() == ()


@pytest.mark.parametrize("rule", ["non-terminal", "open-change-request", "quarantine",
                                  "needs-attention"])  # fmt: skip
def test_every_backing_rule_keeps_its_claim(setup: Setup, rule: str) -> None:
    if rule == "non-terminal":
        fail_reads(setup, {4})  # blocked at the freeze: RUNNING_AGENT across passes
    elif rule == "quarantine":
        setup.runtime.stop_hook = lambda handle: setattr(setup.runtime, "fail_removal", True)
    elif rule == "needs-attention":
        at_read(setup, POINTS["agent-run"], lambda: change(setup, "unlabel"))
        original = setup.tasks.release

        def release(ref: TaskRef) -> None:
            raise ProviderError("down")

        setup.tasks.release = release  # type: ignore[method-assign]
    run(setup)
    own = list(setup.tasks.own_claims(REF))
    assert own, "the scenario holds a claim"
    run(setup)  # the orphan step runs again: every backing rule keeps the claim
    assert setup.tasks.own_claims(REF) == own
    assert setup.store.instance_operations() == ()
    if rule == "needs-attention":
        setup.tasks.release = original  # type: ignore[method-assign]


def test_an_execution_appearing_before_the_effect_stops_the_release(setup: Setup) -> None:
    old = orphan(setup)

    def appear() -> None:
        execution, created = setup.coordinator()._machine.start(REF, execution_id="late",
                                                                 now=setup.clock())  # fmt: skip
        commit(setup, Commit(created=((execution, created),)))

    on_commit(setup.store, operation_is(K.RELEASE_ORPHAN_CLAIM, ST.INTENDED), appear)
    report = run(setup)
    assert "orphaned claim on" in report.summary
    assert "[ledger-integrity]" in report.summary
    (operation,) = setup.store.instance_operations()
    assert operation.status is ST.FAILED
    assert old in setup.tasks.own_claims(REF)  # nothing deleted
    assert not [w for w in setup.tasks.writes if w[0] == "delete-claim"]


def test_a_changed_orphan_is_reported_and_never_retried(setup: Setup) -> None:
    old = orphan(setup)
    setup.tasks.hooks["reread"] = [lambda claim: setattr(old, "instance", "someone-else")]
    report = run(setup)
    assert "[claim-changed]" in report.summary
    (operation,) = setup.store.instance_operations()
    assert operation.status is ST.FAILED
    setup.tasks.hooks.clear()
    run(setup)
    assert len(setup.store.instance_operations()) == 1  # never retried as a delete


# -- orphan release, crash recovery (mandatory) ------------------------------------------------


class Died(BaseException):
    """The process died here."""


@pytest.mark.parametrize("point", ["intent", "effect", "after-effect", "outcome"])
@pytest.mark.parametrize("second", [False, True])
def test_an_orphan_release_survives_crashes_and_never_touches_a_newer_claim(
    tmp_path: Path, point: str, second: bool, sqlite: Callable[[Path], SQLiteExecutionStore]
) -> None:
    db = tmp_path / "executions.sqlite3"
    (tmp_path / "s").mkdir()
    setup = make_setup(tmp_path / "s", store=sqlite(db), tasks=(task("1"),))
    old = orphan(setup)
    setup.tasks.tracked(REF).eligible = False  # keep the task out of the queue
    original_commit = setup.store.commit
    original_release = setup.tasks.release_claim
    crashes = [2 if second else 1]

    def crash_commit(fence: object, changes: Commit, *, context: object = None) -> None:
        orphan_op = [o for o in changes.operations if o.kind is K.RELEASE_ORPHAN_CLAIM]
        if crashes[0] and orphan_op:
            if point == "intent" and orphan_op[0].status is ST.INTENDED:
                crashes[0] -= 1
                raise Died("before the intent")
            if point == "outcome" and orphan_op[0].status.is_final:
                crashes[0] -= 1
                raise Died("before the outcome")
        original_commit(fence, changes, context=context)  # type: ignore[arg-type]

    def crash_release(claim: HeldClaim) -> ReleaseResult:
        if crashes[0] and point == "effect":
            crashes[0] -= 1
            raise Died("before the effect")
        result = original_release(claim)
        if crashes[0] and point == "after-effect":
            crashes[0] -= 1
            raise Died("after the effect")
        return result

    setup.store.commit = crash_commit  # type: ignore[method-assign]
    setup.tasks.release_claim = crash_release  # type: ignore[method-assign]
    for _ in range(2 if second else 1):
        with pytest.raises(Died):
            run(setup)
        setup.ownership.release()
        setup.ownership = Ownership.take(layout=setup.layout, store=setup.store,
                                         instance_id="inst", now=setup.clock())  # fmt: skip
    # A newer claim of this instance on the same task, backed by a live execution.
    newer = FakeClaim("o-newer", "ilmar", "t" + "e" * 31, "ilmar-bot")
    setup.tasks.tracked(REF).claims.append(newer)
    execution, created = setup.coordinator()._machine.start(REF, execution_id="live",
                                                             now=setup.clock())  # fmt: skip
    commit(setup, Commit(created=((execution, created),)))
    deletes_before = [w for w in setup.tasks.writes if w[0] == "delete-claim"]
    setup.tasks.tracked(REF).eligible = True
    run(setup)
    deletes = [w for w in setup.tasks.writes if w[0] == "delete-claim"]
    assert newer in setup.tasks.tracked(REF).claims  # the newer claim survives
    assert {w[2] for w in deletes} <= {old.object_id}  # only the recorded object
    assert len([w for w in deletes if w[2] == old.object_id]) <= 1  # never twice
    assert len(deletes) >= len(deletes_before)
    if point == "intent":  # nothing was durable: nothing is resumed, nothing deleted
        assert setup.store.instance_operations() == ()
        assert deletes == []
        return
    (operation,) = setup.store.instance_operations()
    assert operation.status.is_final  # settled: by observation, or refused by revalidation
    assert dict(operation.intent)["object"] == old.object_id
    check_i11(setup.store)


def test_the_pass_of_an_interrupted_claim_counts_as_its_claim_write(setup: Setup) -> None:
    """§3a: P0 is the pass of the latest commit of the claim operation, so a claim
    interrupted in one pass and completed in the next writes in the later pass."""
    original = setup.tasks.claim
    died = [False]

    def claim_then_die(ref: TaskRef) -> Claim:
        result = original(ref)
        if not died[0]:
            died[0] = True
            raise Died("after the claim was posted, before its outcome")
        return result

    setup.tasks.claim = claim_then_die  # type: ignore[method-assign]
    with pytest.raises(Died):
        run(setup)  # pass A: the claim is posted; its outcome is never recorded
    setup.agent.during = lambda: drop_own_claim(setup)
    run(setup)  # pass B: the claim is repeated (P0 = B); every later read is in P0
    claim = next(o for o in setup.store.operations("e1") if o.kind is K.CLAIM_TASK)
    commits = [e for e in setup.store.events(execution_id="e1") if e.operation == claim.key]
    assert len({e.pass_id for e in commits}) == 2
    assert lost(setup) == []
    assert decisions(setup) == []


ORPHAN_WINDOWS = ["listing", "intent", "reread", "delete", "label"]


def _newer_own(setup: Setup) -> None:
    setup.tasks.tracked(REF).claims.append(FakeClaim("o-newer", "ilmar", "t" + "e" * 31,
                                                     "ilmar-bot"))  # fmt: skip


def _edit(setup: Setup, field: str, value: str) -> None:
    for claim in setup.tasks.tracked(REF).claims:
        if claim.object_id == "o-1":
            setattr(claim, field, value)


ORPHAN_CHANGES: dict[str, Callable[[Setup], object]] = {
    "newer-own-claim": _newer_own,
    "other-instance": lambda s: s.tasks.claim_as(REF, "someone-else"),
    "human": lambda s: setattr(s.tasks.tracked(REF), "human", True),
    "edit-token": lambda s: _edit(s, "token", "t" + "d" * 31),
    "edit-instance": lambda s: _edit(s, "instance", "someone-else"),
    "delete": lambda s: drop_own_claim(s),
    "forged-copy": lambda s: s.tasks.forge(REF, token=s.tasks.own_claims(REF)[0].token
                                           if s.tasks.own_claims(REF) else None),
    "close": lambda s: setattr(s.tasks.tracked(REF), "open", False),
    "gone": lambda s: setattr(s.tasks.tracked(REF), "gone", True),
}  # fmt: skip


@pytest.mark.parametrize("window", ORPHAN_WINDOWS)
@pytest.mark.parametrize("change_", sorted(ORPHAN_CHANGES))
def test_an_orphan_release_acts_only_on_the_observed_object_in_every_window(
    setup: Setup, window: str, change_: str
) -> None:
    """ADR-0026 §5, the mandatory adversarial test at the coordinator: a concurrent
    change between the listing, the intent commit, the re-read, the delete and the
    label step. Only the observed object is ever deleted, while still ours."""
    old = orphan(setup)
    setup.tasks.tracked(REF).eligible = False  # nothing is picked: the orphan step alone
    act = ORPHAN_CHANGES[change_]
    if window == "listing":
        listed = setup.tasks.held_claims

        def held() -> tuple[HeldClaim, ...]:
            claims = tuple(listed())
            act(setup)
            return claims

        setup.tasks.held_claims = held  # type: ignore[method-assign]
    elif window == "intent":
        on_commit(setup.store, operation_is(K.RELEASE_ORPHAN_CLAIM, ST.INTENDED),
                  lambda: act(setup))  # fmt: skip
    else:

        def hook(claim: HeldClaim) -> None:
            act(setup)

        setup.tasks.hooks[window] = [hook]
    others_before = [
        (c.object_id, c.instance, c.token, c.author)
        for c in setup.tasks.tracked(REF).claims
        if c.object_id != old.object_id
    ]
    run(setup)
    deleted = [w[2] for w in setup.tasks.writes if w[0] == "delete-claim"]
    assert set(deleted) <= {old.object_id}  # only the observed object
    assert len(deleted) <= 1
    remaining = {c.object_id: c for c in setup.tasks.tracked(REF).claims}
    for object_id, *_ in others_before:
        assert object_id in remaining  # everything else survives
    if old.object_id not in remaining:  # deleted: it was still ours when re-read
        assert not (window in ("listing", "intent", "reread")
                    and change_ in ("edit-token", "edit-instance"))  # fmt: skip
    tracked = setup.tasks.tracked(REF)
    assert tracked.human is (change_ == "human")  # never touched
    if not tracked.label and not tracked.gone:
        assert not tracked.claims or window == "label"
        assert not tracked.human or window == "label"
    (operation,) = setup.store.instance_operations()
    assert operation.status.is_final
    if operation.status is ST.SUCCEEDED:
        assert operation.result["result"] in ("released", "absent")
    assert dict(operation.intent)["object"] == old.object_id
    check_i11(setup.store)
    assert i13_problems(setup.store, setup.tasks, completed=False) == []
