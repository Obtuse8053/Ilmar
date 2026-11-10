# SPDX-License-Identifier: Apache-2.0
"""Execution records: one attempt by the factory to complete one task.

The allowed state changes live in :mod:`ilmar.core.state_machine`; this module
only defines the data.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from ilmar.domain.tasks import TaskRef


class ExecutionState(enum.Enum):
    QUEUED = "queued"
    """Recorded locally; the task has not been claimed from the provider yet."""

    CLAIMED = "claimed"
    """The task provider has reserved the task for this factory."""

    PROVISIONING = "provisioning"
    """Preparing the workspace: checkout, container start."""

    RUNNING_AGENT = "running_agent"
    """The coding agent is working in the workspace."""

    VERIFYING = "verifying"
    """Independent, deterministic checks are running on the agent's output."""

    PUBLISHING = "publishing"
    """Pushing the branch and opening or updating the change request."""

    RETRY_WAIT = "retry_wait"
    """A retryable failure happened; waiting before the next attempt."""

    SUCCEEDED = "succeeded"
    """Verified changes were published as a change request."""

    FAILED = "failed"
    """Gave up. ``last_error`` explains why."""

    CANCELLED = "cancelled"
    """Stopped on request, or the task stopped being eligible."""

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset(
    {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}
)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Execution:
    """Durable state of one execution.

    ``version`` increases by one with every stored change and is used for
    optimistic concurrency control: a store only accepts an update whose
    ``version`` is exactly one more than the stored record's.
    """

    id: str
    task: TaskRef
    state: ExecutionState
    attempt: int
    version: int
    created_at: datetime
    updated_at: datetime
    branch: str | None = None
    change_request_url: str | None = None
    last_error: str | None = None
    base_revision: str | None = None
    """The revision the current attempt's agent started from (ADR-0010)."""
    candidate_revision: str | None = None
    """The current attempt's frozen output; what is verified and published."""
    retry_not_before: datetime | None = None
    """While in RETRY_WAIT: the earliest time a new attempt may start (ADR-0028,
    #13). Set with the move into RETRY_WAIT, cleared when the wait ends. ``None``
    in RETRY_WAIT only for a wait recorded before schema 7: due at once."""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("id must be non-empty")
        if self.attempt < 1:
            raise ValueError("attempt starts at 1")
        if self.version < 1:
            raise ValueError("version starts at 1")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.candidate_revision is not None and self.base_revision is None:
            raise ValueError("a candidate revision needs a base revision")
        if self.retry_not_before is not None:
            _require_aware(self.retry_not_before, "retry_not_before")
            if self.state is not ExecutionState.RETRY_WAIT:
                raise ValueError("only an execution in retry_wait has a retry_not_before")


@dataclass(frozen=True, slots=True)
class Transition:
    """An entry in an execution's append-only history."""

    execution_id: str
    from_state: ExecutionState | None
    """``None`` for the entry that records creation."""
    to_state: ExecutionState
    at: datetime
    reason: str

    def __post_init__(self) -> None:
        _require_aware(self.at, "at")
