# SPDX-License-Identifier: Apache-2.0
"""In-memory execution store, for tests and dry runs. Not durable.

It follows the same contract as the SQLite store, including fencing, the
operation journal and the resource ledger (ADR-0020): every rule comes from
the shared ``check_fence`` and ``check_commit``. Its event log (ADR-0024)
follows the same rules as the SQLite store's, through ``ilmar.ports.event_log``:
coverage starts at the first claim, with a baseline of what exists.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Sequence
from datetime import datetime

from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    ExecutionNotFoundError,
    FencingError,
)
from ilmar.domain.event_history import StoredState
from ilmar.domain.events import Event, EventContext
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.ledger import Commit, Fence, Operation, OperationStatus, Resource, ResourceStatus
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord
from ilmar.ports import event_log
from ilmar.ports.execution_store import check_apply, check_commit, check_create, check_fence

FROM_SCHEMA_VERSION = 0
"""What the baseline records as the version migrated from: this store has no schema."""


class InMemoryExecutionStore:
    """Thread-safe ``ExecutionStore`` that keeps everything in process memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: dict[str, Execution] = {}
        self._history: dict[str, list[Transition]] = {}
        self._evidence: dict[str, list[str]] = {}  # serialised, so stored copies cannot change
        self._owner: Fence | None = None
        self._operations: dict[str, Operation] = {}
        self._resources: dict[str, Resource] = {}
        self._events: list[str] = []  # canonical lines, so stored events cannot change

    def create(
        self, execution: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        if fence is not None:
            self.commit(fence, Commit(created=((execution, transition),)))
            return
        check_create(execution, transition)
        with self._lock:
            check_fence(self._owner, None)
            self._check_new(execution)
            self._store_new(execution, transition)

    def get(self, execution_id: str) -> Execution | None:
        with self._lock:
            return self._executions.get(execution_id)

    def active_for_task(self, task: TaskRef) -> Execution | None:
        with self._lock:
            return self._active(task)

    def list_in_states(self, states: Collection[ExecutionState]) -> Sequence[Execution]:
        wanted = frozenset(states)
        with self._lock:
            found = [e for e in self._executions.values() if e.state in wanted]
        return sorted(found, key=lambda e: (e.created_at, e.id))

    def apply(
        self, updated: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        if fence is not None:
            self.commit(fence, Commit(applied=((updated, transition),)))
            return
        check_apply(updated, transition)
        with self._lock:
            check_fence(self._owner, None)
            self._check_update(updated, transition)
            self._executions[updated.id] = updated
            self._history[updated.id].append(transition)

    def history(self, execution_id: str) -> Sequence[Transition]:
        with self._lock:
            if execution_id not in self._history:
                raise ExecutionNotFoundError(execution_id)
            return tuple(self._history[execution_id])

    def append_evidence(self, record: EvidenceRecord, *, fence: Fence | None = None) -> None:
        with self._lock:
            check_fence(self._owner, fence)
            if record.execution_id not in self._executions:
                raise ExecutionNotFoundError(record.execution_id)
            events = () if fence is None else (event_log.evidence_event(record, fence),)
            self._evidence.setdefault(record.execution_id, []).append(record.to_json())
            self._append(events)

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        with self._lock:
            if execution_id not in self._executions:
                raise ExecutionNotFoundError(execution_id)
            stored = list(self._evidence.get(execution_id, []))
        return tuple(EvidenceRecord.from_json(text) for text in stored)

    # -- fencing, journal and ledger (ADR-0020) ------------------------------

    def claim_instance(self, instance_id: str, *, now: datetime) -> Fence:
        Fence(instance_id, 1)  # validates the identifier
        with self._lock:
            current = self._owner
            if current is not None and current.instance_id != instance_id:
                raise FencingError(
                    f"this store belongs to instance {current.instance_id!r}, not {instance_id!r}"
                )
            new = Fence(instance_id, 1 if current is None else current.epoch + 1)
            started: tuple[Event, ...] = ()
            if not self._events:  # the first claim starts coverage (ADR-0024 §8)
                started = event_log.baseline(now=now, owner=current, claimant=instance_id,
                                             from_schema_version=FROM_SCHEMA_VERSION,
                                             state=self._stored_state())  # fmt: skip
            acquired = event_log.ownership_event(current, new, now=now)
            self._owner = new
            self._append((*started, acquired))
            return new

    def fence(self) -> Fence | None:
        with self._lock:
            return self._owner

    def commit(self, fence: Fence, changes: Commit, *, context: EventContext | None = None) -> None:
        with self._lock:
            check_fence(self._owner, fence)
            check_commit(
                fence,
                changes,
                execution=self._executions.get,
                operation=self._operations.get,
                resource=self._resources.get,
            )
            events = event_log.commit_events(
                fence,
                changes,
                context,
                execution=self._executions.get,
                operation=self._operations.get,
                resource=self._resources.get,
            )
            new_tasks = [e.task for e, _ in changes.created if not e.state.is_terminal]
            if len(new_tasks) != len(set(new_tasks)):
                raise DuplicateExecutionError("two active executions for one task in one commit")
            for execution, _ in changes.created:
                self._check_new(execution)
            for execution, transition in changes.applied:
                self._check_update(execution, transition)
            # Everything is valid: apply it all (nothing above has changed state).
            for execution, transition in changes.created:
                self._store_new(execution, transition)
            for execution, transition in changes.applied:
                self._executions[execution.id] = execution
                self._history[execution.id].append(transition)
            for operation in changes.operations:
                self._operations[operation.key] = operation
            for resource in changes.resources:
                self._resources[resource.key] = resource
            self._append(events)

    def events(self, *, after_seq: int = 0, limit: int | None = None) -> Sequence[Event]:
        with self._lock:
            lines = self._events[max(after_seq, 0) :]
        if limit is not None:
            lines = lines[:limit]
        return tuple(Event.from_json(line) for line in lines)

    def operation(self, key: str) -> Operation | None:
        with self._lock:
            return self._operations.get(key)

    def operations(self, execution_id: str) -> Sequence[Operation]:
        with self._lock:
            found = [o for o in self._operations.values() if o.execution_id == execution_id]
        return tuple(found)  # insertion order is creation order

    def unfinished_operations(self) -> Sequence[Operation]:
        unfinished = (OperationStatus.INTENDED, OperationStatus.AMBIGUOUS)
        with self._lock:
            return tuple(o for o in self._operations.values() if o.status in unfinished)

    def resource(self, key: str) -> Resource | None:
        with self._lock:
            return self._resources.get(key)

    def resources(
        self,
        *,
        execution_id: str | None = None,
        statuses: Collection[ResourceStatus] | None = None,
    ) -> Sequence[Resource]:
        with self._lock:
            return tuple(
                r
                for r in self._resources.values()
                if (execution_id is None or r.execution_id == execution_id)
                and (statuses is None or r.status in statuses)
            )

    # -- internals ------------------------------------------------------------

    def _append(self, events: Sequence[Event]) -> None:
        """Called last, once every check passed: nothing can fail after it."""
        numbered = event_log.with_seqs(events, len(self._events) + 1)
        self._events.extend(event.to_json() for event in numbered)

    def _stored_state(self) -> StoredState:
        return event_log.stored_state(
            executions=list(self._executions.values()),
            operations=list(self._operations.values()),
            resources=list(self._resources.values()),
            transitions={k: len(v) for k, v in self._history.items()},
            evidence={k: len(v) for k, v in self._evidence.items()},
        )

    def _check_new(self, execution: Execution) -> None:
        if execution.id in self._executions:
            raise DuplicateExecutionError(f"execution id already exists: {execution.id}")
        if not execution.state.is_terminal and self._active(execution.task) is not None:
            raise DuplicateExecutionError(f"task {execution.task} already has an execution")

    def _store_new(self, execution: Execution, transition: Transition) -> None:
        self._executions[execution.id] = execution
        self._history[execution.id] = [transition]

    def _check_update(self, updated: Execution, transition: Transition) -> None:
        current = self._executions.get(updated.id)
        if current is None:
            raise ExecutionNotFoundError(updated.id)
        if current.version != updated.version - 1 or current.state is not transition.from_state:
            raise ConcurrencyConflictError(
                f"execution {updated.id} is at version {current.version}, "
                f"update expected {updated.version - 1}"
            )
        if current.task != updated.task or current.created_at != updated.created_at:
            raise ValueError("task and created_at are immutable")

    def _active(self, task: TaskRef) -> Execution | None:
        for execution in self._executions.values():
            if execution.task == task and not execution.state.is_terminal:
                return execution
        return None
