# SPDX-License-Identifier: Apache-2.0
"""Shared checks for tracker reconciliation (ADR-0026): I11, I12 and I13."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ilmar.core.worker import needs_attention
from ilmar.domain.event_history import StoredState, verify
from ilmar.domain.events import ActorKind, Event, EventType
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Fence,
    OperationKind,
    OperationStatus,
    ResourceKind,
    ResourceStatus,
)
from ilmar.domain.tasks import TaskRef
from ilmar.ports.execution_store import ExecutionStore

from fakes import FakeTaskProvider

S = ExecutionState
K = OperationKind
R = ResourceStatus
_OPERATION_EVENTS = {
    EventType.OPERATION_INTENDED,
    EventType.OPERATION_SUCCEEDED,
    EventType.OPERATION_FAILED,
    EventType.OPERATION_AMBIGUOUS,
    EventType.OPERATION_RECONCILED,
}


def check_i11(store: ExecutionStore) -> None:
    executions = store.list_in_states(set(ExecutionState))
    stored = StoredState(
        executions=executions,
        operations=[
            *(o for e in executions for o in store.operations(e.id)),
            *store.instance_operations(),
        ],
        resources=store.resources(),
        transitions={e.id: len(store.history(e.id)) for e in executions},
        evidence={e.id: len(store.evidence(e.id)) for e in executions},
    )
    report = verify(store.events(), stored)
    assert report.failures == (), report.failures


def i12_problems(store: ExecutionStore, execution_id: str) -> list[str]:
    """ADR-0025 I12: once a cancellation is accepted, no agent run, freeze,
    verification run or first push starts."""
    events = store.events(execution_id=execution_id)
    accepted = [e.seq or 0 for e in events if e.type is EventType.EXECUTION_CANCEL_REQUESTED
                and e.data.get("decision") == "accepted"]  # fmt: skip
    if not accepted:
        return []
    forbidden = (K.RUN_AGENT, K.FREEZE_CANDIDATE, K.VERIFY, K.PUBLISH_REVISION)
    late = [
        str(e.operation)
        for e in events
        if e.type is EventType.OPERATION_INTENDED
        and (e.seq or 0) > accepted[0]
        and any(str(e.operation).startswith(k.value + ":") for k in forbidden)
    ]
    return [f"I12: started after the cancellation was accepted: {late}"] if late else []


def backed(store: ExecutionStore, ref: TaskRef) -> bool:
    """ADR-0026 §5, rule 3, read independently of the coordinator's own code."""
    for execution in store.list_in_states(set(ExecutionState)):
        if execution.task != ref:
            continue
        if not execution.state.is_terminal or needs_attention(store, execution):
            return True
        rows = store.resources(execution_id=execution.id)
        if any(r.status is R.QUARANTINED for r in rows):
            return True
        if execution.state is S.SUCCEEDED and any(
            r.kind is ResourceKind.CHANGE_REQUEST and r.status in (R.ACTIVE, R.RELEASING)
            for r in rows
        ):
            return True
    return False


def i13_problems(
    store: ExecutionStore,
    tasks: FakeTaskProvider,
    *,
    completed: bool = True,
    others: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """ADR-0026 I13, "Claims follow the store".

    * after a completed pass, every authentic claim of this instance is backed;
    * an orphan release deletes only the object recorded in its intent;
    * a label restore never posts a claim (and never decides: no decision shares
      its commit, checked through the pass and seq of each decision);
    * no ``task-claim-lost`` cancellation without an earlier, uncleared
      ``observed`` from a pass other than the decision's and the claim write's,
      after that write in ``seq``;
    * claims of others (``others``: ``(task, object)``) are never deleted.
    """
    problems: list[str] = []
    if completed:
        for claim in tasks.held_claims():
            if not backed(store, claim.ref):
                problems.append(f"I13: an unbacked claim remains on {claim.ref}")
    recorded = {
        o.intent.get("object")
        for o in store.instance_operations()
        if o.kind is K.RELEASE_ORPHAN_CLAIM
    }
    claimed_objects: set[str] = set()
    for what, _task, obj in tasks.writes:
        if what == "post-claim":
            claimed_objects.add(obj)
    restores = sum(1 for e in store.list_in_states(set(ExecutionState))
                   for o in store.operations(e.id) if o.kind is K.RESTORE_CLAIM_LABEL)  # fmt: skip
    claims = sum(1 for e in store.list_in_states(set(ExecutionState))
                 for o in store.operations(e.id) if o.kind is K.CLAIM_TASK)  # fmt: skip
    posts = sum(1 for what, _, _ in tasks.writes if what == "post-claim")
    if restores and posts > claims:
        problems.append(f"I13: {posts} claims posted by {claims} claim operations")
    for what, task, obj in tasks.writes:
        if what == "delete-claim" and obj not in claimed_objects and obj not in recorded:
            problems.append(f"I13: deleted {obj} on {task}, never ours nor recorded")
    for task, obj in others:
        if obj and all(c.object_id != obj for ref in tasks.refs()
                       for c in tasks.tracked(ref).claims if str(ref) == task):  # fmt: skip
            problems.append(f"I13: another party's claim {obj} on {task} was removed")
    for execution in store.list_in_states(set(ExecutionState)):
        problems += lost_claim_problems(store, execution)
    problems += stray_label_problems(store, tasks)
    return problems


def stray_label_problems(store: ExecutionStore, tasks: FakeTaskProvider) -> list[str]:
    """ADR-0027's clause of I13: a stray label is removed only by a recorded
    ``clear-stray-label`` operation, never in the pass of its intent, and never
    while an execution of the task was active (created before the removal and not
    yet terminal), whatever the tracker showed."""
    problems: list[str] = []
    events = store.events()
    sweeps = [o for o in store.instance_operations() if o.kind is K.CLEAR_STRAY_LABEL]
    cleared = [o for o in sweeps if o.result.get("result") in ("cleared", "absent")]
    writes = [w for w in tasks.writes if w[0] == "clear-label"]
    # One removal at most per recorded sweep. A sweep whose outcome was lost in a
    # crash re-reads on recovery and settles ABSENT, so it need not say "cleared".
    if len(writes) > len([o for o in sweeps if o.status is not OperationStatus.FAILED]):
        problems.append(f"I13: {len(writes)} label removals for {len(sweeps)} sweeps")
    for operation in cleared:
        mine = [e for e in events if e.operation == operation.key]
        intents = [e for e in mine if e.type is EventType.OPERATION_INTENDED]
        outcome = [e for e in mine if e.type is EventType.OPERATION_SUCCEEDED]
        if not intents or not outcome:
            problems.append(f"I13: {operation.key} has no intent or no outcome")
            continue
        if outcome[-1].pass_id == intents[0].pass_id:
            problems.append(f"I13: {operation.key} acted in the pass of its intent")
        at = outcome[-1].seq or 0
        task = operation.intent.get("task")
        for execution in store.list_in_states(set(ExecutionState)):
            if str(execution.task) != task:
                continue
            own = store.events(execution_id=execution.id)
            created = min((e.seq or 0) for e in own)
            ended = [
                e.seq or 0
                for e in own
                if e.type is EventType.EXECUTION_TRANSITIONED
                and ExecutionState(str(e.data.get("to"))).is_terminal
            ]
            if created < at and (not ended or min(ended) > at):
                problems.append(f"I13: {operation.key} cleared beside active {execution.id}")
    return problems


def lost_claim_problems(store: ExecutionStore, execution: Execution) -> list[str]:
    events = sorted(store.events(execution_id=execution.id), key=lambda e: e.seq or 0)
    claim = next((o for o in store.operations(execution.id) if o.kind is K.CLAIM_TASK), None)
    problems = []
    for decision in events:
        if not (
            decision.type is EventType.EXECUTION_CANCEL_REQUESTED
            and decision.reason == "task-claim-lost"
            and decision.actor.kind is ActorKind.COORDINATOR
        ):
            continue
        before = [e for e in events if (e.seq or 0) < (decision.seq or 0)]
        writes = [e for e in before if e.type in _OPERATION_EVENTS and claim is not None
                  and e.operation == claim.key]  # fmt: skip
        if not writes:
            problems.append(f"{execution.id}: a lost claim decided without a claim write")
            continue
        p0 = writes[-1]
        lost = [e for e in before if e.type is EventType.EXECUTION_CLAIM_LOST]
        # The open observation the decision confirmed: the last observed before the
        # decision's own commit, with no clearing between it and that commit.
        same_commit = [e for e in lost if e.pass_id == decision.pass_id
                       and e.data.get("state") == "cleared"
                       and (e.seq or 0) == (decision.seq or 0) - 1]  # fmt: skip
        earlier = [e for e in lost if e not in same_commit]
        if not earlier or earlier[-1].data.get("state") != "observed":
            problems.append(f"{execution.id}: a lost claim decided without an open observation")
            continue
        observed = earlier[-1]
        if len({observed.pass_id, decision.pass_id, p0.pass_id}) != 3:
            problems.append(f"{execution.id}: the passes P0, P1, P2 are not distinct")
        if not (p0.seq or 0) < (observed.seq or 0) < (decision.seq or 0):
            problems.append(f"{execution.id}: P0, P1 and P2 are out of order")
        if not same_commit:
            problems.append(f"{execution.id}: the decision did not clear the observation")
    decisions = [e for e in events if e.type is EventType.EXECUTION_CANCEL_REQUESTED
                 and e.data.get("decision") == "accepted"]  # fmt: skip
    if len(decisions) > 1:
        problems.append(f"{execution.id}: {len(decisions)} accepted cancellations")
    opened = 0
    for event in events:
        if event.type is EventType.EXECUTION_CLAIM_LOST:
            opened = opened + 1 if event.data.get("state") == "observed" else 0
            if opened > 1:
                problems.append(f"{execution.id}: two open observations")
    return problems


def on_commit(store: Any, when: Callable[[Commit], bool], action: Callable[[], object]) -> None:
    """Run ``action`` once, right after the first commit for which ``when`` holds."""
    original = store.commit
    done: list[bool] = []

    def commit(fence: Fence, changes: Commit, *, context: Any = None) -> None:
        original(fence, changes, context=context)
        if not done and when(changes):
            done.append(True)
            action()

    store.commit = commit


def operation_is(kind: OperationKind, status: OperationStatus) -> Callable[[Commit], bool]:
    def test(changes: Commit) -> bool:
        return any(o.kind is kind and o.status is status for o in changes.operations)

    return test


def tracker_events(store: ExecutionStore, execution_id: str = "e1") -> list[Event]:
    kinds = {EventType.EXECUTION_CANCEL_REQUESTED, EventType.EXECUTION_CLAIM_LOST}
    return [e for e in store.events(execution_id=execution_id) if e.type in kinds]
