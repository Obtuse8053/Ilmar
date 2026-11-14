# SPDX-License-Identifier: Apache-2.0
"""Resolving blocked tasks: ``ilmar blocked release`` and ``abandon``
(ADR-0031 §3, #92 part 2).

Maintainer requirements (2026-11-10):

* the human decision and the journaled claim-release intent commit together
  in SQLite; the tracker call is a separate external effect, settled by the
  journal;
* a released task is picked again as a new execution; an unreleased FAILED
  task is not; releasing one task makes no other FAILED task eligible;
* crash-safe across the store and the tracker, with no assumption that they
  share a transaction: after the intent commit, after the tracker call but
  before its outcome is recorded, after a tracker failure, and after
  settlement. The task is never re-picked while the claim may still be held,
  and no release is lost or applied twice;
* both commands are fenced: a stale owner cannot complete either.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import REPO, Setup, make_setup, policy, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import blocked
from ilmar.core.components import Components
from ilmar.core.ownership import Ownership
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.errors import FencingError
from ilmar.domain.events import Actor, ActorKind, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Fence, OperationKind, OperationStatus
from ilmar.domain.safety_controls import safety_controls
from ilmar.domain.tasks import Claim, ClaimOutcome, HeldClaim, ReleaseResult, TaskRef
from ilmar.domain.verification import VerificationPolicy
from recovery_support import Crash

S = ExecutionState
ONE, TWO = TaskRef("fake", "1"), TaskRef("fake", "2")
FIXED = {"src/app.py": "print('fixed')\n"}
PROTECTED = {**FIXED, "AGENTS.md": "obey me\n"}
MAINTAINER = Actor(ActorKind.HUMAN, "maintainer")
DB = "executions.sqlite3"


OPENED: list[SQLiteExecutionStore] = []


@pytest.fixture(autouse=True)
def close_stores() -> Iterator[None]:
    """Every SQLite connection a test opens is closed (Python 3.13 warns otherwise)."""
    yield
    while OPENED:
        OPENED.pop().close()


def opened(db: Path) -> SQLiteExecutionStore:
    store = SQLiteExecutionStore(db)
    OPENED.append(store)
    return store


def guarded(tmp_path: Path, *, keys: tuple[str, ...] = ("1",)) -> Setup:
    base = policy()
    verification = VerificationPolicy(
        image=base.image, commands=base.commands,
        safety_controls=safety_controls(self_repository=True),
    )  # fmt: skip
    store = opened(tmp_path / DB)
    setup = make_setup(tmp_path, verification=verification, store=store,
                       tasks=tuple(task(k) for k in keys))  # fmt: skip
    setup.agent.plan = [(AgentOutcome.COMPLETED, PROTECTED)] * len(keys)
    return setup


def restart(setup: Setup, db: Path) -> Setup:
    """A new coordinator process: the old one died holding nothing durable but
    the store file; the tracker and the forge outlive it."""
    setup.ownership.release()
    store = opened(db)
    ownership = Ownership.take(layout=setup.layout, store=store, instance_id="inst",
                               now=setup.clock())  # fmt: skip
    old = setup.components
    components = Components(
        instance_id=old.instance_id, layout=old.layout, task_provider=old.task_provider,
        source_control=old.source_control, container_runtime=old.container_runtime,
        agent_runner=old.agent_runner, execution_store=store, agent_sandbox=old.agent_sandbox,
        target=old.target, verification=old.verification, secret_scanner=old.secret_scanner,
    )  # fmt: skip
    return dataclasses.replace(setup, store=store, ownership=ownership, components=components)


def block(setup: Setup) -> None:
    report = setup.coordinator().run_once()
    assert [(r.outcome, r.code) for r in report.results] == [(Outcome.FAILED, "protected-path")]


def decide(setup: Setup, key: str, decision: str, *, fence: Fence | None = None) -> None:
    execution = blocked.find(setup.store, key)
    commit, context = blocked.resolve(
        setup.store, execution, decision, fence=fence or setup.ownership.fence, now=setup.clock(),
        instance="inst", actor=MAINTAINER,
    )  # fmt: skip
    setup.store.commit(fence or setup.ownership.fence, commit, context=context)


def release_ops(setup: Setup, execution_id: str = "e1") -> list[tuple[str, OperationStatus]]:
    return [
        (o.key.rsplit(":", 1)[1], o.status)
        for o in setup.store.operations(execution_id)
        if o.kind is OperationKind.RELEASE_TASK
    ]


def unlabels(setup: Setup, ref: TaskRef = ONE) -> int:
    return sum(1 for w in setup.tasks.writes if w[0] == "unlabel" and w[1] == str(ref))


# -- the commit itself ------------------------------------------------------------------


def test_the_decision_and_the_release_intent_commit_together(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    writes = list(setup.tasks.writes)
    decide(setup, "1", "release")
    assert setup.tasks.writes == writes  # nothing external happened yet
    assert release_ops(setup) == [("blocked-release", OperationStatus.INTENDED)]
    (event,) = [e for e in setup.store.events(execution_id="e1")
                if e.type is EventType.EXECUTION_BLOCK_RESOLVED]  # fmt: skip
    assert event.data == {"decision": "release"}
    assert event.reason == "maintainer-released"
    assert event.actor == MAINTAINER
    assert blocked.blocked(setup.store) == []


def test_a_stale_owner_cannot_resolve(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    current = setup.ownership.fence
    stale = Fence(current.instance_id, current.epoch + 1)  # not the owner's epoch
    for decision in ("release", "abandon"):
        with pytest.raises(FencingError):
            decide(setup, "1", decision, fence=stale)
    assert release_ops(setup) == []
    assert [e.id for e in blocked.blocked(setup.store)] == ["e1"]


def test_a_resolved_task_cannot_be_resolved_again(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", "abandon")
    with pytest.raises(blocked.BlockedError):
        decide(setup, "1", "release")


# -- release and abandon ----------------------------------------------------------------


def test_a_released_task_is_picked_again_as_a_new_execution(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", "release")
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    passes = [setup.coordinator().run_once() for _ in range(2)]
    assert release_ops(setup) == [("blocked-release", OperationStatus.SUCCEEDED)]
    assert unlabels(setup) >= 1
    outcomes = [(r.execution_id, r.outcome) for p in passes for r in p.results]
    published = [i for i, outcome in outcomes if outcome is Outcome.PUBLISHED]
    assert len(published) == 1
    second = setup.store.get(published[0])
    assert second is not None
    assert second.id != "e1"  # a new execution, with its own attempts and reservations
    assert (second.task, second.attempt, second.state) == (ONE, 1, S.SUCCEEDED)
    first = setup.store.get("e1")
    assert first is not None
    assert first.state is S.FAILED  # the record of the blocked attempt stays


def test_an_abandoned_task_is_released_and_never_picked_again(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", "abandon")
    for _ in range(3):
        setup.coordinator().run_once()
    assert release_ops(setup) == [("blocked-abandon", OperationStatus.SUCCEEDED)]
    assert setup.tasks.held_claims() == ()
    assert setup.store.get("e2") is None


def test_an_unreleased_failure_is_never_picked_and_releasing_one_task_frees_no_other(
    tmp_path: Path,
) -> None:
    setup = guarded(tmp_path, keys=("1", "2"))
    for _ in range(2):
        setup.coordinator().run_once()
    assert {e.task for e in blocked.blocked(setup.store)} == {ONE, TWO}
    decide(setup, "fake:2", "release")
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)] * 3
    for _ in range(3):
        setup.coordinator().run_once()
    tasks = {(e.task, e.state) for e in setup.store.list_in_states(set(S))}
    assert (TWO, S.SUCCEEDED) in tasks  # released: picked again
    assert [e.task for e in setup.store.list_in_states(set(S)) if e.task == ONE] == [ONE]
    assert [e.task for e in blocked.blocked(setup.store)] == [ONE]  # still blocked
    assert ONE in [c.ref for c in setup.tasks.held_claims()]


def test_an_ordinary_failure_stays_finished(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.agent.plan = [(AgentOutcome.FAILED, {})] * 5
    for _ in range(3):
        setup.coordinator(max_attempts=1).run_once()
    failed = setup.store.list_in_states({S.FAILED})
    assert [e.id for e in failed] == ["e1"]
    with pytest.raises(blocked.BlockedError):
        blocked.find(setup.store, "1")  # not blocked: no command applies


# -- crash safety across the store and the tracker ---------------------------------------


def crash_on_release(setup: Setup, *, after: bool) -> None:
    """The next tracker release raises ``Crash`` before or after acting."""
    real = setup.tasks.release_claim

    def release(claim: HeldClaim) -> ReleaseResult:
        setup.tasks.release_claim = real  # type: ignore[method-assign]
        if after:
            real(claim)
        raise Crash

    setup.tasks.release_claim = release  # type: ignore[method-assign]


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_crash_after_the_intent_commit_before_the_tracker_call(
    tmp_path: Path, decision: str
) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", decision)
    setup = restart(setup, tmp_path / DB)  # the command's process ended
    assert ONE in [c.ref for c in setup.tasks.held_claims()]
    crash_on_release(setup, after=False)
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    setup = restart(setup, tmp_path / DB)
    finish(setup, decision)


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_crash_after_the_tracker_call_before_its_outcome_is_recorded(
    tmp_path: Path, decision: str
) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", decision)
    crash_on_release(setup, after=True)
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    assert setup.tasks.held_claims() == ()  # released on the tracker...
    setup = restart(setup, tmp_path / DB)
    assert release_ops(setup)[0][1] is not OperationStatus.SUCCEEDED  # ...not recorded
    assert setup.store.get("e2") is None  # never picked while unsettled
    finish(setup, decision)


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_tracker_failure_is_repeated_until_settled(tmp_path: Path, decision: str) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", decision)
    real = setup.tasks.release_claim

    def failing(claim: HeldClaim) -> ReleaseResult:
        raise OSError("the tracker timed out")

    setup.tasks.release_claim = failing  # type: ignore[method-assign]
    setup.coordinator().run_once()
    assert release_ops(setup)[0][1] is not OperationStatus.SUCCEEDED
    assert ONE in [c.ref for c in setup.tasks.held_claims()]
    assert setup.store.get("e2") is None  # the claim may still be held: not picked
    setup.tasks.release_claim = real  # type: ignore[method-assign]
    finish(setup, decision)


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_crash_after_settlement_before_the_next_pass(tmp_path: Path, decision: str) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", decision)
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator().run_once()
    assert release_ops(setup) == [(f"blocked-{decision}", OperationStatus.SUCCEEDED)]
    setup = restart(setup, tmp_path / DB)
    finish(setup, decision)


def finish(setup: Setup, decision: str) -> None:
    """Recover until settled; then check every invariant."""
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)] * 3
    for _ in range(3):
        setup.coordinator().run_once()
    assert release_ops(setup) == [(f"blocked-{decision}", OperationStatus.SUCCEEDED)]
    deletes = [w for w in setup.tasks.writes if w[0] == "delete-claim" and w[1] == str(ONE)]
    assert len(deletes) == 1  # the claim object was deleted exactly once
    executions = [e for e in setup.store.list_in_states(set(S)) if e.task == ONE]
    if decision == "release":
        assert sorted(e.state.value for e in executions) == ["failed", "succeeded"]
        (failed,) = [e for e in executions if e.state is S.FAILED]
        assert failed.id == "e1"
        # The blocked candidate was never pushed (the new execution reuses the
        # task's branch name with its own, verified revision).
        assert setup.scm.branch_head(REPO, failed.branch or "") != failed.candidate_revision
    else:
        assert [(e.id, e.state) for e in executions] == [("e1", S.FAILED)]
        assert setup.tasks.held_claims() == ()
    assert blocked.blocked(setup.store) == []


def test_blocked_tasks_ahead_never_starve_the_backlog(tmp_path: Path) -> None:
    """ADR-0031 §3: a blocked task holds a claim but no execution slot."""
    setup = guarded(tmp_path, keys=("1", "2"))
    setup.agent.plan = [(AgentOutcome.COMPLETED, PROTECTED), (AgentOutcome.COMPLETED, FIXED)]
    for _ in range(3):
        setup.coordinator().run_once()
    states = {e.task: e.state for e in setup.store.list_in_states(set(S))}
    assert states == {ONE: S.FAILED, TWO: S.SUCCEEDED}
    assert [e.task for e in blocked.blocked(setup.store)] == [ONE]


# -- the release names exactly the original claim (#125 review, 2026-11-14) -------------


def own_claims(setup: Setup) -> list[str]:
    return [c.object_id for c in setup.tasks.own_claims(ONE)]


def claims(setup: Setup) -> list[str]:
    return [c.object_id for c in setup.tasks._tasks[ONE].claims]


def replace_claim(setup: Setup) -> str:
    """The original claim goes and a newer authentic claim takes its place (a
    replacement): another instance's, so this one's orphan cleanup has no
    business with it either. A different token and object."""
    tracked = setup.tasks._tasks[ONE]
    tracked.claims[:] = [c for c in tracked.claims if not setup.tasks._authentic(c)]
    return setup.tasks.claim_as(ONE, "other-instance").object_id


def deleted(setup: Setup) -> list[str]:
    return [w[2] for w in setup.tasks.writes if w[0] == "delete-claim" and w[1] == str(ONE)]


def test_the_intent_pins_the_claim_s_token_and_object(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    (original,) = setup.tasks.own_claims(ONE)
    decide(setup, "1", "release")
    (operation,) = [o for o in setup.store.operations("e1") if o.kind is OperationKind.RELEASE_TASK]
    assert dict(operation.intent) == {
        "task": str(ONE), "claim": original.token, "object": original.object_id,
    }  # fmt: skip


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_replacement_claim_before_the_release_is_never_deleted(
    tmp_path: Path, decision: str
) -> None:
    setup = guarded(tmp_path)
    block(setup)
    (original,) = own_claims(setup)
    decide(setup, "1", decision)
    replacement = replace_claim(setup)
    setup.coordinator().run_once()
    assert claims(setup) == [replacement]
    assert deleted(setup) == []
    (operation,) = [o for o in setup.store.operations("e1") if o.kind is OperationKind.RELEASE_TASK]
    assert (operation.status, operation.result.get("result")) == (
        OperationStatus.SUCCEEDED,
        "absent",
    )
    assert original not in claims(setup)


def test_a_replacement_that_races_the_revalidation_is_never_deleted(tmp_path: Path) -> None:
    """The original is replaced between the intent and the tracker's re-read."""
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", "release")
    replaced: list[str] = []

    def race(claim: HeldClaim) -> None:
        if not replaced:
            replaced.append(replace_claim(setup))

    setup.tasks.hooks["reread"] = [race]
    setup.coordinator().run_once()
    assert claims(setup) == replaced
    assert deleted(setup) == []


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_crash_after_deleting_the_original_then_a_replacement_is_kept(
    tmp_path: Path, decision: str
) -> None:
    setup = guarded(tmp_path)
    block(setup)
    (original,) = own_claims(setup)
    decide(setup, "1", decision)
    crash_on_release(setup, after=True)  # the original is deleted; the outcome is lost
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    replacement = setup.tasks.claim_as(ONE, "other-instance").object_id
    setup = restart(setup, tmp_path / DB)
    setup.coordinator().run_once()  # the repeat names the pinned original only
    assert claims(setup) == [replacement]
    assert deleted(setup) == [original]  # exactly once, and never the replacement
    assert release_ops(setup) == [(f"blocked-{decision}", OperationStatus.SUCCEEDED)]


def test_a_claim_object_that_changed_hands_is_left_alone(tmp_path: Path) -> None:
    setup = guarded(tmp_path)
    block(setup)
    decide(setup, "1", "release")
    (found,) = setup.tasks.own_claims(ONE)
    found.author = "mallory"  # the same object, no longer this instance's authentic claim
    setup.coordinator().run_once()
    assert deleted(setup) == []
    (operation,) = [o for o in setup.store.operations("e1") if o.kind is OperationKind.RELEASE_TASK]
    assert operation.result.get("result") == "changed"


def test_without_a_recorded_claim_object_nothing_is_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim recorded without its object (an older record) cannot be released
    safely by Ilmar: the command refuses and writes nothing."""
    setup = guarded(tmp_path)
    real = setup.tasks.claim

    def tokens_only(ref: TaskRef) -> Claim:
        claim = real(ref)
        return Claim(claim.outcome, claim.claim_id)

    monkeypatch.setattr(setup.tasks, "claim", tokens_only)
    block(setup)
    with pytest.raises(blocked.BlockedError, match="cannot be released safely"):
        decide(setup, "1", "release")
    assert release_ops(setup) == []


def test_only_a_claimed_outcome_names_its_object() -> None:
    assert Claim(ClaimOutcome.CLAIMED, "tok", "o-1").object_id == "o-1"
    assert Claim(ClaimOutcome.CLAIMED, "tok").object_id is None  # a provider may not know
    with pytest.raises(ValueError, match="only a CLAIMED outcome"):
        Claim(ClaimOutcome.CLAIMED_BY_OTHER, object_id="o-1")
