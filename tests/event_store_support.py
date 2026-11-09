# SPDX-License-Identifier: Apache-2.0
"""A store as the previous schema left it after a crash (ADR-0024 §9), shared by
the migration tests and the ``ilmar events`` tests."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    changed_paths_digest,
)
from ilmar.domain.tasks import TaskRef

from contracts.execution_store import evidence_record
from helpers import at

MACHINE = ExecutionStateMachine(max_attempts=3)
S, K, ST, R = ExecutionState, OperationKind, OperationStatus, ResourceStatus
BASE, CANDIDATE = "a" * 40, "b" * 40

# Every non-terminal state, plus a succeeded execution with its claim held and a
# failed one (ADR-0024 §9).
PATHS: dict[str, tuple[ExecutionState, ...]] = {
    "q": (),
    "c": (S.CLAIMED,),
    "p": (S.CLAIMED, S.PROVISIONING),
    "r": (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT),
    "v": (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING),
    "u": (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING, S.PUBLISHING),
    "w": (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.RETRY_WAIT),
    "ok": (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING, S.PUBLISHING, S.SUCCEEDED),
    "bad": (S.CLAIMED, S.FAILED),
}


def freeze(fence: Fence, execution_id: str) -> tuple[Operation, Operation]:
    first = Operation.intended(K.FREEZE_CANDIDATE, execution_id, 1, fence=fence, now=at(3),
                               intent={"what": "freeze"})  # fmt: skip
    done = first.moved(ST.SUCCEEDED, fence=fence, now=at(3), result={
        "revision": CANDIDATE, "base_revision": BASE,
        "changed_paths_digest": changed_paths_digest(["src/a.py"]), "changed_path_count": "1",
    })  # fmt: skip
    return first, done


def drive(store: SQLiteExecutionStore, fence: Fence, execution_id: str) -> Execution:
    execution, transition = MACHINE.start(TaskRef("fake", execution_id), execution_id=execution_id,
                                          now=at(1))  # fmt: skip
    store.create(execution, transition, fence=fence)
    for n, target in enumerate(PATHS[execution_id], 2):
        extra: dict[str, str] = {}
        ops: tuple[Operation, ...] = ()
        if target is S.RUNNING_AGENT:
            extra["base_revision"] = BASE
        if target is S.VERIFYING:
            extra["candidate_revision"] = CANDIDATE
            first, done = freeze(fence, execution_id)
            store.commit(fence, Commit(operations=(first,)))
            ops = (done,)
        if target in (S.FAILED, S.RETRY_WAIT):
            extra["error"] = "gave up"
        updated, step = MACHINE.advance(execution, target, now=at(n), reason="r", **extra)
        store.commit(fence, Commit(applied=((updated, step),), operations=ops))
        execution = updated
    return execution


def populate(store: SQLiteExecutionStore) -> Fence:
    """What the previous schema holds after a crash (ADR-0024 §9)."""
    store.claim_instance("inst", now=at(0))
    fence = store.claim_instance("inst", now=at(0))  # a store owned before: epoch 2
    for execution_id in PATHS:
        drive(store, fence, execution_id)
    store.append_evidence(evidence_record("v", seconds=5), fence=fence)
    claim = Operation.intended(K.CLAIM_TASK, "ok", 1, fence=fence, now=at(2))
    store.commit(fence, Commit(operations=(claim,)))
    store.commit(fence, Commit(operations=(claim.moved(ST.SUCCEEDED, fence=fence, now=at(2)),)))
    # A crash left these unfinished.
    run = Operation.intended(K.RUN_AGENT, "r", 1, fence=fence, now=at(6))
    start = Operation.intended(K.START_CONTAINER, "r", 1, fence=fence, now=at(6), step="box")
    store.commit(fence, Commit(operations=(run, start)))
    store.commit(fence, Commit(operations=(start.moved(ST.AMBIGUOUS, fence=fence, now=at(7)),)))
    resources = []
    for name, statuses in (("active", (R.ACTIVE,)), ("releasing", (R.ACTIVE, R.RELEASING)),
                           ("quarantined", (R.QUARANTINED,))):  # fmt: skip
        row = Resource.intended(ResourceKind.CONTAINER, "r", 1, name, owner_token="t", now=at(6))
        store.commit(fence, Commit(resources=(row,)))
        for status in statuses:
            row = row.moved(
                status, now=at(7), identity="id-" + name if status is R.ACTIVE else None
            )
            store.commit(fence, Commit(resources=(row,)))
        resources.append(row)
    return fence


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())
