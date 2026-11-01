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
