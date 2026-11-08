# SPDX-License-Identifier: Apache-2.0
"""The store side of the event log (ADR-0024 §2 and §8), shared by every store adapter.

A store calls these inside its own transaction:

* :func:`commit_events` after :func:`~ilmar.ports.execution_store.check_commit`
  and before it writes a row. It derives the commit's mandatory events, fills
  each ``data.from`` from the stored row, and checks the pairing (layer 2):
  exactly one event per changed row and no event without one. Any mismatch
  raises ``LedgerIntegrityError``, so the transaction rolls back.
* :func:`evidence_event` and :func:`ownership_event` for the two writes that
  do not go through a :class:`~ilmar.domain.ledger.Commit`.
* :func:`baseline` when event coverage starts: the migration's snapshot of
  what exists, with its own count check.

Nothing here writes; the adapter appends the events, with their ``seq``, in
the same transaction as the rows.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from ilmar.domain.errors import LedgerIntegrityError
from ilmar.domain.event_history import StoredState
from ilmar.domain.events import (
    COORDINATOR,
    ActorKind,
    BaselineExecution,
    Event,
    EventContext,
    EventEnvelopeError,
    EventType,
    baseline_events,
    derive_events,
    resource_ref,
)
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import Commit, Fence, Operation, Resource
from ilmar.domain.verification import EvidenceRecord

_EXECUTION_EVENTS = frozenset({EventType.EXECUTION_CREATED, EventType.EXECUTION_TRANSITIONED})
_OPERATION_EVENTS = frozenset(
    {
        EventType.OPERATION_INTENDED,
        EventType.OPERATION_SUCCEEDED,
        EventType.OPERATION_FAILED,
        EventType.OPERATION_AMBIGUOUS,
        EventType.OPERATION_RECONCILED,
    }
)
_RESOURCE_EVENTS = frozenset(
    {EventType.RESOURCE_RECORDED, EventType.RESOURCE_CHANGED, EventType.RESOURCE_QUARANTINED}
)
MANDATORY_TYPES = _EXECUTION_EVENTS | _OPERATION_EVENTS | _RESOURCE_EVENTS
"""The event types :func:`derive_events` produces, one per changed row."""


def default_context(fence: Fence, changes: Commit) -> EventContext:
    """The context of a commit whose caller supplied none.

    The time is the latest of the commit's own row times, which come from the
    caller's clock (ADR-0022), so the store never reads a clock itself.
    """
    times = [e.updated_at for e, _ in (*changes.created, *changes.applied)]
    times += [o.updated_at for o in changes.operations]
    times += [r.updated_at for r in changes.resources]
    if not times:
        raise ValueError("an empty commit has no events")
    return EventContext(at=max(times), instance=fence.instance_id, epoch=fence.epoch)


def commit_events(
    fence: Fence,
    changes: Commit,
    context: EventContext | None,
    *,
    execution: Callable[[str], Execution | None],
    operation: Callable[[str], Operation | None],
    resource: Callable[[str], Resource | None],
) -> tuple[Event, ...]:
    """The events ``changes`` must be committed with, ``data.from`` filled and paired.

    The lookups return the **stored** rows, before this commit. Raises
    ``LedgerIntegrityError`` for a context that does not belong to ``fence``,
    an invalid envelope (ADR-0024 §7) or any pairing mismatch.
    """
    if changes.empty:
        return ()
    if context is None:
        context = default_context(fence, changes)
    _check_context(fence, context)
    try:
        derived = list(derive_events(changes, context))
        position = len(changes.created) + len(changes.applied)
        for new_operation in changes.operations:
            stored_operation = operation(new_operation.key)
            if stored_operation is not None:
                derived[position] = derived[position].with_previous(stored_operation.status.value)
            position += 1
        for new_resource in changes.resources:
            stored_resource = resource(new_resource.key)
            if stored_resource is not None:
                derived[position] = derived[position].with_previous(stored_resource.status.value)
            position += 1
    except EventEnvelopeError as error:
        raise LedgerIntegrityError(f"an event of this commit is invalid: {error}") from None
    events = tuple(derived)
    check_pairing(changes, events, execution=execution, operation=operation, resource=resource)
    return events


def _check_context(fence: Fence, context: EventContext) -> None:
    if (context.instance, context.epoch) != (fence.instance_id, fence.epoch):
        raise LedgerIntegrityError("the event context belongs to another owner than the fence")
    if context.actor.kind is ActorKind.MIGRATION:
        raise LedgerIntegrityError("only the migration writes migration events")


def check_pairing(
    changes: Commit,
    events: Sequence[Event],
    *,
    execution: Callable[[str], Execution | None],
    operation: Callable[[str], Operation | None],
    resource: Callable[[str], Resource | None],
) -> None:
    """Exactly one mandatory event per changed row, matching its key, new version and
    new status, with ``from`` equal to the stored row's status; and no mandatory
    event without a changed row (ADR-0024 §2, layer 2). Raises ``LedgerIntegrityError``.
    """
    expected: dict[tuple[str, str], tuple[EventType | None, int, str, str | None]] = {}
    for e, _ in changes.created:
        expected[("execution", e.id)] = (EventType.EXECUTION_CREATED, e.version, e.state.value,
                                         None)  # fmt: skip
    for e, transition in changes.applied:
        stored_execution = execution(e.id)
        source = transition.from_state
        if stored_execution is None or source is None or stored_execution.state is not source:
            raise LedgerIntegrityError(f"execution {e.id}: the transition's source is not stored")
        expected[("execution", e.id)] = (EventType.EXECUTION_TRANSITIONED, e.version,
                                         e.state.value, source.value)  # fmt: skip
    for o in changes.operations:
        stored_operation = operation(o.key)
        previous = None if stored_operation is None else stored_operation.status.value
        expected[("operation", o.key)] = (None, o.version, o.status.value, previous)
    for r in changes.resources:
        stored_resource = resource(r.key)
        previous = None if stored_resource is None else stored_resource.status.value
        expected[("resource", resource_ref(r.key))] = (None, r.version, r.status.value, previous)
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event.type not in MANDATORY_TYPES:
            raise LedgerIntegrityError(f"{event.type.value} is not a mandatory event of a commit")
        key, status = _subject(event)
        if key not in expected:
            raise LedgerIntegrityError(f"{event.type.value} for {key[1]} pairs with no changed row")
        if key in seen:
            raise LedgerIntegrityError(f"a second event for {key[0]} {key[1]}")
        seen.add(key)
        kind, version, new_status, before = expected[key]
        if kind is not None and event.type is not kind:
            raise LedgerIntegrityError(
                f"{key[0]} {key[1]} has {event.type.value}, not {kind.value}"
            )
        if event.data.get("version") != version or status != new_status:
            raise LedgerIntegrityError(f"the event for {key[0]} {key[1]} does not match its row")
        if event.data.get("from") != before:
            raise LedgerIntegrityError(
                f"the event for {key[0]} {key[1]} does not start from the stored status"
            )
    missing = expected.keys() - seen
    if missing:
        raise LedgerIntegrityError(f"changed rows without an event: {sorted(missing)}")


def _subject(event: Event) -> tuple[tuple[str, str], str]:
    data = event.data
    if event.type in _EXECUTION_EVENTS:
        status = data.get("to") if event.type is EventType.EXECUTION_TRANSITIONED else data.get(
            "state")  # fmt: skip
        return ("execution", str(event.execution)), str(status)
    if event.type in _OPERATION_EVENTS:
        return ("operation", str(event.operation)), str(data.get("status"))
    return ("resource", str(data.get("ref"))), str(data.get("status"))


def evidence_event(
    record: EvidenceRecord, fence: Fence, context: EventContext | None = None
) -> Event:
    """``evidence.recorded`` for an appended evidence record. ``context`` supplies
    the pass and the actor; its owner must be ``fence``'s."""
    if context is not None:
        _check_context(fence, context)
    data: dict[str, object] = {"digest": record.digest(), "verdict": record.verdict.value,
                               "policy_digest": record.policy_digest}  # fmt: skip
    if record.run is not None:
        data["run"] = record.run
    try:
        return Event.create(EventType.EVIDENCE_RECORDED, at=record.recorded_at,
                            instance=fence.instance_id, epoch=fence.epoch,
                            actor=COORDINATOR if context is None else context.actor,
                            pass_id=None if context is None else context.pass_id,
                            execution=record.execution_id, attempt=record.attempt,
                            run=record.run, data=data)  # fmt: skip
    except EventEnvelopeError as error:
        raise LedgerIntegrityError(f"the evidence event is invalid: {error}") from None


def ownership_event(previous: Fence | None, fence: Fence, *, now: datetime) -> Event:
    """``ownership.acquired``: the new owner records the takeover (ADR-0024 §6)."""
    data: dict[str, object] = {}
    if previous is not None:
        data = {"previous_instance": previous.instance_id, "previous_epoch": previous.epoch}
    return Event.create(EventType.OWNERSHIP_ACQUIRED, at=now, instance=fence.instance_id,
                        epoch=fence.epoch, actor=COORDINATOR, data=data)  # fmt: skip


def baseline(
    *,
    now: datetime,
    owner: Fence | None,
    claimant: str,
    from_schema_version: int,
    state: StoredState,
) -> tuple[Event, ...]:
    """The migration baseline (ADR-0024 §8) of ``state``, the rows that exist now.

    The epoch is the stored owner's, or 0 for a store never owned. The result
    is checked against ``state`` before it is returned: one marker, then
    exactly one baseline per execution, operation and resource, with the
    marker's counts. Raises ``LedgerIntegrityError`` otherwise.
    """
    events = baseline_events(
        at=now,
        instance=claimant if owner is None else owner.instance_id,
        epoch=0 if owner is None else owner.epoch,
        from_schema_version=from_schema_version,
        executions=[
            BaselineExecution(e, state.transitions.get(e.id, 0), state.evidence.get(e.id, 0))
            for e in state.executions
        ],
        operations=list(state.operations),
        resources=list(state.resources),
    )
    marker, *rest = events
    covered: dict[EventType, tuple[str, set[str], list[object]]] = {
        EventType.EXECUTION_BASELINE: ("executions", {e.id for e in state.executions},
                                       [e.execution for e in rest
                                        if e.type is EventType.EXECUTION_BASELINE]),
        EventType.OPERATION_BASELINE: ("operations", {o.key for o in state.operations},
                                       [e.operation for e in rest
                                        if e.type is EventType.OPERATION_BASELINE]),
        EventType.RESOURCE_BASELINE: ("resources", {resource_ref(r.key) for r in state.resources},
                                      [e.data.get("ref") for e in rest
                                       if e.type is EventType.RESOURCE_BASELINE]),
    }  # fmt: skip
    if marker.type is not EventType.COVERAGE_STARTED:
        raise LedgerIntegrityError("the baseline does not start with its coverage marker")
    for name, keys, found in covered.values():
        if marker.data.get(name) != len(keys) or len(found) != len(keys) or set(found) != keys:
            raise LedgerIntegrityError(f"the baseline does not cover every stored {name[:-1]}")
    if len(rest) != sum(len(keys) for _, keys, _ in covered.values()):
        raise LedgerIntegrityError("the baseline holds an event that is not a snapshot")
    return events


def with_seqs(events: Sequence[Event], first: int) -> tuple[Event, ...]:
    """``events`` numbered from ``first``, in order."""
    return tuple(e.with_seq(first + n) for n, e in enumerate(events))


def stored_state(
    *,
    executions: Sequence[Execution],
    operations: Sequence[Operation],
    resources: Sequence[Resource],
    transitions: dict[str, int],
    evidence: dict[str, int],
) -> StoredState:
    """A :class:`StoredState`, ordered as the baseline orders it: oldest first."""
    return StoredState(
        executions=tuple(sorted(executions, key=lambda e: (e.created_at, e.id))),
        operations=tuple(operations),
        resources=tuple(resources),
        transitions=dict(transitions),
        evidence=dict(evidence),
    )
