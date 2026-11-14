# SPDX-License-Identifier: Apache-2.0
"""Blocked tasks and their human resolution (ADR-0031 §3, #92 part 2).

A candidate that touched a protected safety control fails with
``protected-path``. Its task keeps Ilmar's claim and is *blocked*: it is
never retried, picked again or published. Only a maintainer resolves it:

* ``release``: the claim is released, and the FAILED execution is marked as
  released, so the task may be picked again as a **new** execution;
* ``abandon``: the claim is released, and the task stays out of the backlog.

The decision is recorded durably, as an ``execution.block_resolved`` event by
the maintainer, **in the same commit** as the journaled intent of the claim
release (an operation keyed ``release-task:<execution>:<attempt>:blocked-<decision>``).
The intent **pins the claim** this execution took: its token and the
tracker's id of the object holding it, both recorded when it was claimed.
The tracker call itself is an external effect: the worker performs it in a
later pass through the journal, from that stored intent only, and repeats it
after a crash or an ambiguous outcome. It deletes only that object, after
revalidating its token, so a newer authentic claim (a replacement) is never
deleted. Until it succeeds the execution needs attention, so its task is
never picked while the claim may still be held.

The commit is fenced like any other: only the instance's owner can write it,
so it cannot be made while a coordinator runs, and a stale owner cannot
complete it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ilmar.core.effects import claim_intent
from ilmar.domain.events import BLOCK_DECISIONS, Actor, Event, EventContext, EventType
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Commit, Fence, Operation, OperationKind, OperationStatus
from ilmar.domain.tasks import ClaimOutcome, HeldClaim
from ilmar.ports.execution_store import ExecutionStore

PROTECTED_PATH = "protected-path"
REASONS = {"release": "maintainer-released", "abandon": "maintainer-abandoned"}


class BlockedError(ValueError):
    """The command cannot apply: nothing blocked matches, or it is already resolved."""


def step(decision: str) -> str:
    """The journal step of the claim release a decision implies."""
    if decision not in BLOCK_DECISIONS:
        raise ValueError(f"not a decision: {decision!r}")
    return f"blocked-{decision}"


def failed_for_protected_path(store: ExecutionStore, execution: Execution) -> bool:
    """A FAILED execution whose candidate touched a safety control."""
    if execution.state is not ExecutionState.FAILED:
        return False
    for event in reversed(store.events(execution_id=execution.id)):
        if (
            event.type is EventType.EXECUTION_TRANSITIONED
            and event.data.get("to") == ExecutionState.FAILED.value
        ):
            return event.reason == PROTECTED_PATH
    return False


def resolution(store: ExecutionStore, execution: Execution) -> str | None:
    """The maintainer's decision on this blocked execution, if any."""
    for event in store.events(execution_id=execution.id):
        if event.type is EventType.EXECUTION_BLOCK_RESOLVED:
            decision = event.data.get("decision")
            return decision if isinstance(decision, str) else None
    return None


def blocked(store: ExecutionStore) -> list[Execution]:
    """Every blocked execution (unresolved), by task key."""
    found = [
        e
        for e in store.list_in_states({ExecutionState.FAILED})
        if failed_for_protected_path(store, e) and resolution(store, e) is None
    ]
    return sorted(found, key=lambda e: (e.task.provider, e.task.key, e.id))


def protected_paths(store: ExecutionStore, execution: Execution) -> str:
    """What the failure recorded about the paths (its last error)."""
    return execution.last_error or ""


def find(store: ExecutionStore, task: str) -> Execution:
    """The blocked execution of ``task`` (``provider:key`` or just ``key``)."""
    matches = [e for e in blocked(store) if task in (e.task.key, f"{e.task.provider}:{e.task.key}")]
    if not matches:
        raise BlockedError(f"no blocked task matches {task!r} (see 'ilmar blocked list')")
    if len({(e.task.provider, e.task.key) for e in matches}) > 1:
        raise BlockedError(f"{task!r} matches more than one task: name it as provider:key")
    return matches[-1]


def pinned_claim(store: ExecutionStore, execution: Execution) -> HeldClaim:
    """The claim this execution took, as recorded when it claimed: its token and
    the tracker object holding it. Without both, nothing can be released safely."""
    for operation in store.operations(execution.id):
        if (
            operation.kind is OperationKind.CLAIM_TASK
            and operation.status is OperationStatus.SUCCEEDED
            and operation.result.get("outcome") == ClaimOutcome.CLAIMED.value
        ):
            token = operation.result.get("claim_id")
            obj = operation.result.get("object_id")
            if token and obj:
                return HeldClaim(execution.task, token, obj)
            raise BlockedError(
                f"{execution.id}: the claim's tracker object was not recorded when it was "
                "taken, so it cannot be released safely by Ilmar; release it by hand"
            )
    raise BlockedError(f"{execution.id}: no claim of this execution is recorded")


def resolve(
    store: ExecutionStore,
    execution: Execution,
    decision: str,
    *,
    fence: Fence,
    now: datetime,
    instance: str,
    actor: Actor,
) -> tuple[Commit, EventContext]:
    """The one fenced commit that records ``decision`` and the intent of the claim
    release it implies, pinned to the claim this execution took. Nothing external
    happens here."""
    claim = pinned_claim(store, execution)
    intent = Operation.intended(
        OperationKind.RELEASE_TASK, execution.id, execution.attempt,
        fence=fence, now=now, step=step(decision), intent=claim_intent(claim),
    )  # fmt: skip
    event = Event.create(
        EventType.EXECUTION_BLOCK_RESOLVED,
        at=now,
        instance=instance,
        epoch=fence.epoch,
        actor=actor,
        execution=execution.id,
        attempt=execution.attempt,
        reason=REASONS[decision],
        data={"decision": decision},
    )
    context = EventContext(
        at=now, instance=instance, epoch=fence.epoch, actor=actor,
        reasons={intent.key: REASONS[decision]},
    )  # fmt: skip
    commit = Commit(
        operations=(intent,),
        extra_events=(event,),
        expected=((execution.id, execution.version),),
    )
    return commit, context


def release_settled(store: ExecutionStore, execution: Execution) -> bool:
    """Whether the claim release a resolution implies has succeeded."""
    decision = resolution(store, execution)
    if decision is None:
        return False
    for operation in store.operations(execution.id):
        if (
            operation.kind is OperationKind.RELEASE_TASK
            and operation.key.endswith(":" + step(decision))
            and operation.status is OperationStatus.SUCCEEDED
        ):
            return True
    return False


def released(store: ExecutionStore, executions: Sequence[Execution]) -> set[str]:
    """Ids of FAILED executions a maintainer released, whose claim release is done:
    the pick ignores them, so their task may be picked again."""
    return {
        e.id for e in executions if resolution(store, e) == "release" and release_settled(store, e)
    }
