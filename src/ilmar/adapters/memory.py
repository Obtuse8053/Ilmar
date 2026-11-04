# SPDX-License-Identifier: Apache-2.0
"""In-memory execution store, for tests and dry runs. Not durable."""

from __future__ import annotations

import threading
from collections.abc import Collection, Sequence

from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    ExecutionNotFoundError,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord
from ilmar.ports.execution_store import check_apply, check_create


class InMemoryExecutionStore:
    """Thread-safe ``ExecutionStore`` that keeps everything in process memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: dict[str, Execution] = {}
        self._history: dict[str, list[Transition]] = {}
        self._evidence: dict[str, list[str]] = {}  # serialised, so stored copies cannot change

    def create(self, execution: Execution, transition: Transition) -> None:
        check_create(execution, transition)
        with self._lock:
            if execution.id in self._executions:
                raise DuplicateExecutionError(f"execution id already exists: {execution.id}")
            if not execution.state.is_terminal and self._active(execution.task) is not None:
                raise DuplicateExecutionError(f"task {execution.task} already has an execution")
            self._executions[execution.id] = execution
            self._history[execution.id] = [transition]

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

    def apply(self, updated: Execution, transition: Transition) -> None:
        check_apply(updated, transition)
        with self._lock:
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
            self._executions[updated.id] = updated
            self._history[updated.id].append(transition)

    def history(self, execution_id: str) -> Sequence[Transition]:
        with self._lock:
            if execution_id not in self._history:
                raise ExecutionNotFoundError(execution_id)
            return tuple(self._history[execution_id])

    def append_evidence(self, record: EvidenceRecord) -> None:
        with self._lock:
            if record.execution_id not in self._executions:
                raise ExecutionNotFoundError(record.execution_id)
            self._evidence.setdefault(record.execution_id, []).append(record.to_json())

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        with self._lock:
            if execution_id not in self._executions:
                raise ExecutionNotFoundError(execution_id)
            stored = list(self._evidence.get(execution_id, []))
        return tuple(EvidenceRecord.from_json(text) for text in stored)

    def _active(self, task: TaskRef) -> Execution | None:
        for execution in self._executions.values():
            if execution.task == task and not execution.state.is_terminal:
                return execution
        return None
