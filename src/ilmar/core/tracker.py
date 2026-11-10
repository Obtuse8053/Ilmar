# SPDX-License-Identifier: Apache-2.0
"""Tracker reconciliation: what the task's standing decides (ADR-0026 §3, §3a).

Everything here reads durable events and builds the events a decision records.
Nothing reads the tracker or performs an effect: the worker that holds the
execution's lease reads the standing and commits what these functions build,
in one fenced commit (ADR-0025 §4).

**Confirming a lost claim (§3a).** Three distinct passes, ordered by the store,
never by a clock (ADR-0022 §Time):

* ``P0``, the pass of the latest ``operation.*`` event of the execution's
  ``claim`` operation (an intent, a repeated intent or an outcome);
* ``P1``, a later pass whose fresh ``CLAIM_LOST`` read recorded ``observed``;
* ``P2``, a later pass still whose fresh ``CLAIM_LOST`` read confirms it.

Only event sequence numbers, pass ids and claim ids take part; no timestamp is
read by any function in this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ilmar.domain.events import Actor, ActorKind, Event, EventContext, EventType
from ilmar.domain.ledger import Operation, OperationKind, OperationStatus
from ilmar.domain.tasks import ClaimOutcome

LOST = "task-claim-lost"
COORDINATOR = Actor(ActorKind.COORDINATOR)
_OPERATION_EVENTS = frozenset(
    {
        EventType.OPERATION_INTENDED,
        EventType.OPERATION_SUCCEEDED,
        EventType.OPERATION_FAILED,
        EventType.OPERATION_AMBIGUOUS,
        EventType.OPERATION_RECONCILED,
        EventType.OPERATION_BASELINE,
    }
)
REPICKABLE = frozenset(
    {
        "task-closed",
        "task-unlabelled",
        "task-excluded",
        "task-claimed-by-human",
        "task-claimed-by-other-instance",
    }
)
"""Tracker reasons after which the task may be picked again, as a **new**
execution, once the tracker lists it as eligible (ADR-0026 §3). Never after a
person's cancellation, a lost claim, or a vanished task."""

EXPLANATIONS = {
    "cancel-requested": "its cancellation was requested",
    "task-closed": "the issue was closed",
    "task-unlabelled": "the issue is no longer labelled for Ilmar",
    "task-excluded": "the issue was labelled to exclude it",
    "task-claimed-by-human": "a person claimed it",
    "task-claimed-by-other-instance": "another claim appeared on it",
    "task-gone": "the issue no longer exists here",
    "task-claim-lost": "Ilmar's claim on it was removed",
}


@dataclass(frozen=True, slots=True)
class ClaimWrite:
    """The execution's ``claim`` operation and its latest commit (``P0``)."""

    operation: Operation
    seq: int
    pass_id: str | None

    @property
    def claim_id(self) -> str | None:
        """The claim this execution holds, if its claim succeeded and recorded one."""
        if self.operation.status is not OperationStatus.SUCCEEDED:
            return None
        if self.operation.result.get("outcome") != ClaimOutcome.CLAIMED.value:
            return None
        return self.operation.result.get("claim_id")


def claim_write(events: Sequence[Event], operations: Sequence[Operation]) -> ClaimWrite | None:
    """The execution's claim operation and the event of its latest commit."""
    claims = [o for o in operations if o.kind is OperationKind.CLAIM_TASK]
    if not claims:
        return None
    operation = claims[-1]
    latest = [e for e in events if e.type in _OPERATION_EVENTS and e.operation == operation.key]
    if not latest:
        return None
    last = max(latest, key=_seq)
    return ClaimWrite(operation, _seq(last), last.pass_id)


def open_observation(events: Sequence[Event]) -> Event | None:
    """The open first observation: the latest ``execution.claim_lost``, if ``observed``."""
    lost = [e for e in events if e.type is EventType.EXECUTION_CLAIM_LOST]
    if not lost:
        return None
    latest = max(lost, key=_seq)
    return latest if latest.data.get("state") == "observed" else None


def observed_in_pass(events: Sequence[Event], pass_id: str | None) -> bool:
    """Whether this pass already recorded an ``observed`` (at most one per pass)."""
    return pass_id is not None and any(
        e.type is EventType.EXECUTION_CLAIM_LOST
        and e.data.get("state") == "observed"
        and e.pass_id == pass_id
        for e in events
    )


def may_observe(write: ClaimWrite | None, pass_id: str | None) -> bool:
    """A first observation may be recorded: the claim succeeded with an id, and this
    pass is not ``P0`` (the claim write's own lag window is never observed)."""
    return (
        write is not None
        and write.claim_id is not None
        and pass_id is not None
        and write.pass_id != pass_id
    )


def confirms(
    observation: Event, write: ClaimWrite | None, events: Sequence[Event], pass_id: str | None
) -> bool:
    """Rules 3 to 5 of §3a, from event order and identities only.

    3. recorded in another pass, before every event of this pass;
    4. after the latest claim write, and in a pass other than ``P0``;
    5. about the claim the execution holds.

    Rule 1 (a fresh ``CLAIM_LOST`` read) and rule 6 (no publish intent) are the
    caller's; rule 2 is ``observation`` itself.
    """
    if write is None or write.claim_id is None or pass_id is None:
        return False
    seq = _seq(observation)
    this_pass = [_seq(e) for e in events if e.pass_id == pass_id]
    if observation.pass_id in (None, pass_id) or (this_pass and seq >= min(this_pass)):
        return False
    if seq <= write.seq or observation.pass_id == write.pass_id:
        return False
    return observation.data.get("claim") == write.claim_id


def stale(observation: Event, write: ClaimWrite | None) -> bool:
    """An open observation that can never confirm (rules 4 and 5 fail for good)."""
    if write is None or write.claim_id is None:
        return True
    return (
        _seq(observation) <= write.seq
        or observation.pass_id == write.pass_id
        or observation.data.get("claim") != write.claim_id
    )


def claim_lost(
    context: EventContext,
    execution_id: str,
    *,
    state: str,
    claim: str,
    observed: str | None = None,
) -> Event:
    """``execution.claim_lost``: a first observation, or its clearing."""
    data = {"state": state, "claim": claim}
    if observed is not None:
        data["observed"] = observed
    return Event.create(
        EventType.EXECUTION_CLAIM_LOST,
        at=context.at,
        instance=context.instance,
        epoch=context.epoch,
        actor=COORDINATOR,
        pass_id=context.pass_id,
        execution=execution_id,
        reason=LOST,
        data=data,
    )


def decision(context: EventContext, execution_id: str, *, reason: str, decided: str) -> Event:
    """``execution.cancel_requested`` decided by the coordinator from the tracker."""
    return Event.create(
        EventType.EXECUTION_CANCEL_REQUESTED,
        at=context.at,
        instance=context.instance,
        epoch=context.epoch,
        actor=COORDINATOR,
        pass_id=context.pass_id,
        execution=execution_id,
        reason=reason,
        data={"decision": decided, "observed": reason},
    )


def accepted_reason(events: Sequence[Event]) -> str | None:
    """The reason code of the first accepted cancellation (ADR-0026 §4): the code
    the ``CANCELLED`` transition carries."""
    for event in sorted(events, key=_seq):
        if (
            event.type is EventType.EXECUTION_CANCEL_REQUESTED
            and event.data.get("decision") == "accepted"
        ):
            return event.reason
    return None


def _seq(event: Event) -> int:
    if event.seq is None:  # pragma: no cover - stored events always carry one
        raise ValueError("an event read from the store has a seq")
    return event.seq
