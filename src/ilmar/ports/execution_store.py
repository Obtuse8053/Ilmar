# SPDX-License-Identifier: Apache-2.0
"""Port for durable execution state."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord


class ExecutionStore(Protocol):
    """Persists executions and their transition history.

    The store does not decide which transitions are legal; that is the state
    machine's job. It guarantees, atomically:

    * at most one non-terminal execution per task
      (``create`` raises ``DuplicateExecutionError``);
    * no lost updates: ``apply`` succeeds only when the stored version is
      ``updated.version - 1`` (otherwise ``ConcurrencyConflictError``);
    * the execution record and its history entry are written together or not
      at all;
    * transitions and evidence records are append-only: there is no way to
      change or remove one, and evidence read back is checked against its
      digest (``EvidenceIntegrityError``).

    Inputs must satisfy :func:`check_create` / :func:`check_apply`.
    """

    def create(self, execution: Execution, transition: Transition) -> None: ...

    def get(self, execution_id: str) -> Execution | None: ...

    def active_for_task(self, task: TaskRef) -> Execution | None: ...

    def list_in_states(self, states: Collection[ExecutionState]) -> Sequence[Execution]:
        """Executions in any of ``states``, oldest first (ties broken by id)."""
        ...

    def apply(self, updated: Execution, transition: Transition) -> None:
        """Replace the stored execution with ``updated`` and append ``transition``.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def history(self, execution_id: str) -> Sequence[Transition]:
        """All transitions in the order they were stored.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def append_evidence(self, record: EvidenceRecord) -> None:
        """Append a verification evidence record (ADR-0010, ADR-0018).

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        """All evidence records of an execution, in the order they were appended.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...


def check_create(execution: Execution, transition: Transition) -> None:
    """Validate arguments to :meth:`ExecutionStore.create`. Raises ``ValueError``."""
    if execution.version != 1:
        raise ValueError("a new execution must have version 1")
    if transition.from_state is not None:
        raise ValueError("the creation transition must have from_state=None")
    _check_matches(execution, transition)


def check_apply(updated: Execution, transition: Transition) -> None:
    """Validate arguments to :meth:`ExecutionStore.apply`. Raises ``ValueError``."""
    if updated.version < 2:
        raise ValueError("an updated execution must have version >= 2")
    if transition.from_state is None:
        raise ValueError("an update transition must have a from_state")
    _check_matches(updated, transition)


def _check_matches(execution: Execution, transition: Transition) -> None:
    if transition.execution_id != execution.id:
        raise ValueError("transition belongs to a different execution")
    if transition.to_state is not execution.state:
        raise ValueError("transition target does not match the execution state")
    if transition.at != execution.updated_at:
        raise ValueError("transition time must equal the execution's updated_at")
