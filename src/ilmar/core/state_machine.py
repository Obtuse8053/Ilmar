# SPDX-License-Identifier: Apache-2.0
"""The execution workflow state machine.

All execution state changes go through :class:`ExecutionStateMachine`. It is a
pure function of its inputs: it never reads the clock or touches storage. The
caller persists the returned ``(Execution, Transition)`` pair with
``ExecutionStore.create`` or ``ExecutionStore.apply``.

Happy path::

    QUEUED -> CLAIMED -> PROVISIONING -> RUNNING_AGENT -> VERIFYING
           -> PUBLISHING -> SUCCEEDED

From PROVISIONING onwards a retryable failure moves to RETRY_WAIT, which
returns to PROVISIONING with the attempt counter incremented. Every
non-terminal state can move to FAILED or CANCELLED.

Candidate revisions (ADR-0010): ``base_revision`` is recorded on entering
RUNNING_AGENT and ``candidate_revision`` on entering VERIFYING; VERIFYING and
PUBLISHING cannot be entered without them, they cannot be changed within an
attempt, and a new attempt (RETRY_WAIT -> PROVISIONING) clears both.

Retry backoff (ADR-0028, #13): entering RETRY_WAIT records
``retry_not_before``; RETRY_WAIT -> PROVISIONING is refused before that time,
and leaving RETRY_WAIT clears it. The caller computes the time
(:class:`RetryBackoff`) and supplies ``now``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType

from ilmar.domain.errors import InvalidTransitionError, RetryLimitExceededError
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import TaskRef

_S = ExecutionState
_STOP = frozenset({_S.FAILED, _S.CANCELLED})
_RETRYABLE = frozenset({_S.RETRY_WAIT})

ALLOWED_TRANSITIONS: Mapping[ExecutionState, frozenset[ExecutionState]] = MappingProxyType(
    {
        _S.QUEUED: frozenset({_S.CLAIMED}) | _STOP,
        _S.CLAIMED: frozenset({_S.PROVISIONING}) | _STOP,
        _S.PROVISIONING: frozenset({_S.RUNNING_AGENT}) | _RETRYABLE | _STOP,
        _S.RUNNING_AGENT: frozenset({_S.VERIFYING}) | _RETRYABLE | _STOP,
        _S.VERIFYING: frozenset({_S.PUBLISHING}) | _RETRYABLE | _STOP,
        _S.PUBLISHING: frozenset({_S.SUCCEEDED}) | _RETRYABLE | _STOP,
        _S.RETRY_WAIT: frozenset({_S.PROVISIONING}) | _STOP,
        _S.SUCCEEDED: frozenset(),
        _S.FAILED: frozenset(),
        _S.CANCELLED: frozenset(),
    }
)

_REQUIRES_ERROR = frozenset({_S.FAILED, _S.RETRY_WAIT})


@dataclasses.dataclass(frozen=True, slots=True)
class RetryBackoff:
    """Capped exponential backoff between attempts (F-002, ADR-0028).

    The wait after attempt ``n`` fails is ``base * 2 ** (n - 1)``, at most
    ``max``. No jitter: one coordinator owns an instance, so there is no herd
    to spread, and a deterministic wait is testable.
    """

    base_seconds: float
    max_seconds: float

    def __post_init__(self) -> None:
        if self.base_seconds < 0:
            raise ValueError("base_seconds must not be negative")
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds must not be less than base_seconds")

    def delay(self, failed_attempt: int) -> timedelta:
        if failed_attempt < 1:
            raise ValueError("attempts start at 1")
        exponent = min(failed_attempt - 1, 62)  # 2**62 seconds is beyond any cap
        return timedelta(seconds=min(self.max_seconds, self.base_seconds * 2**exponent))

    def not_before(self, now: datetime, failed_attempt: int) -> datetime:
        return now + self.delay(failed_attempt)


NO_BACKOFF = RetryBackoff(0.0, 0.0)
"""Retry at once. Tests only: configuration requires a positive base."""


def retry_wait_reason(
    execution: Execution, now: datetime, *, high_water: datetime | None
) -> str | None:
    """Why a new attempt of ``execution`` (in RETRY_WAIT) may not start at ``now``,
    or ``None`` if it may.

    Both the execution's ``retry_not_before`` and the store's high-water mark
    (the latest persisted time) must lie in the past: a clock that stepped back
    only makes the wait longer (ADR-0028 §3).
    """
    if execution.state is not _S.RETRY_WAIT:
        raise ValueError(f"execution {execution.id} is not waiting to retry")
    if high_water is not None and now < high_water:
        return (
            f"the clock ({now.isoformat()}) is behind the latest persisted time "
            f"({high_water.isoformat()}); waiting for it to catch up"
        )
    due = execution.retry_not_before
    if due is not None and now < due:
        return f"retry not before {due.isoformat()}"
    return None


def can_transition(source: ExecutionState, target: ExecutionState) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


class ExecutionStateMachine:
    """Validates and applies execution state changes.

    ``max_attempts`` bounds how many times an execution may enter
    PROVISIONING. With ``max_attempts=1`` no retries are allowed.
    """

    def __init__(self, *, max_attempts: int) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._max_attempts = max_attempts

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def start(
        self,
        task: TaskRef,
        *,
        execution_id: str,
        now: datetime,
        reason: str = "task discovered",
    ) -> tuple[Execution, Transition]:
        """Create a new execution in QUEUED."""
        _require_reason(reason)
        execution = Execution(
            id=execution_id,
            task=task,
            state=_S.QUEUED,
            attempt=1,
            version=1,
            created_at=now,
            updated_at=now,
        )
        transition = Transition(
            execution_id=execution_id,
            from_state=None,
            to_state=_S.QUEUED,
            at=now,
            reason=reason,
        )
        return execution, transition

    def retries_remaining(self, execution: Execution) -> int:
        return max(0, self._max_attempts - execution.attempt)

    def advance(
        self,
        execution: Execution,
        target: ExecutionState,
        *,
        now: datetime,
        reason: str,
        error: str | None = None,
        branch: str | None = None,
        change_request_url: str | None = None,
        base_revision: str | None = None,
        candidate_revision: str | None = None,
        retry_not_before: datetime | None = None,
    ) -> tuple[Execution, Transition]:
        """Move ``execution`` to ``target``.

        ``error`` is required when entering FAILED or RETRY_WAIT. ``branch`` and
        ``change_request_url``, when given, are recorded on the execution;
        ``None`` keeps the current value. ``retry_not_before`` is given only
        when entering RETRY_WAIT, and never earlier than ``now``.

        Raises ``InvalidTransitionError`` (or its subclass
        ``RetryLimitExceededError``) if the change is not allowed, and
        ``ValueError`` for malformed arguments.
        """
        _require_reason(reason)
        source = execution.state
        if not can_transition(source, target):
            raise InvalidTransitionError(
                f"execution {execution.id}: {source.value} -> {target.value} is not allowed"
            )
        if target in _REQUIRES_ERROR and not error:
            raise ValueError(f"entering {target.value} requires an error description")
        if now < execution.updated_at:
            raise ValueError("now must not precede the execution's last update")

        attempt = execution.attempt
        if target is _S.RETRY_WAIT and attempt >= self._max_attempts:
            raise RetryLimitExceededError(
                f"execution {execution.id} has used all {self._max_attempts} attempts"
            )
        if retry_not_before is not None:
            if target is not _S.RETRY_WAIT:
                raise ValueError("retry_not_before is set only on entering retry_wait")
            if retry_not_before < now:
                raise ValueError("retry_not_before must not precede now")
        if (
            source is _S.RETRY_WAIT
            and target is _S.PROVISIONING
            and execution.retry_not_before is not None
            and now < execution.retry_not_before
        ):
            raise InvalidTransitionError(
                f"execution {execution.id} may not retry before "
                f"{execution.retry_not_before.isoformat()}"
            )
        if source is _S.RETRY_WAIT and target is _S.PROVISIONING:
            attempt += 1
            if attempt > self._max_attempts:
                raise RetryLimitExceededError(
                    f"execution {execution.id} has used all {self._max_attempts} attempts"
                )

        base, candidate = _revisions(execution, target, base_revision, candidate_revision)

        updated = dataclasses.replace(
            execution,
            state=target,
            attempt=attempt,
            version=execution.version + 1,
            updated_at=now,
            branch=branch if branch is not None else execution.branch,
            change_request_url=(
                change_request_url
                if change_request_url is not None
                else execution.change_request_url
            ),
            last_error=error if error is not None else execution.last_error,
            base_revision=base,
            candidate_revision=candidate,
            retry_not_before=retry_not_before if target is _S.RETRY_WAIT else None,
        )
        transition = Transition(
            execution_id=execution.id,
            from_state=source,
            to_state=target,
            at=now,
            reason=reason,
        )
        return updated, transition


def _revisions(
    execution: Execution,
    target: ExecutionState,
    base_revision: str | None,
    candidate_revision: str | None,
) -> tuple[str | None, str | None]:
    """The revisions after the transition; ``ValueError`` if the rules are broken."""
    if execution.state is _S.RETRY_WAIT and target is _S.PROVISIONING:
        if base_revision or candidate_revision:
            raise ValueError("a new attempt starts without revisions")
        return None, None  # a new attempt never inherits a candidate
    base, candidate = execution.base_revision, execution.candidate_revision
    if base_revision is not None:
        if target is not _S.RUNNING_AGENT or base is not None:
            raise ValueError("base_revision is set once, on entering running_agent")
        base = _revision(base_revision, "base_revision")
    if candidate_revision is not None:
        if target is not _S.VERIFYING or candidate is not None:
            raise ValueError("candidate_revision is set once, on entering verifying")
        candidate = _revision(candidate_revision, "candidate_revision")
    if target in (_S.VERIFYING, _S.PUBLISHING) and (base is None or candidate is None):
        raise ValueError(f"entering {target.value} requires a base and a candidate revision")
    if target is _S.RUNNING_AGENT and base is None:
        raise ValueError("entering running_agent requires the base revision")
    return base, candidate


def _revision(value: str, name: str) -> str:
    if not value or not value.isalnum():
        raise ValueError(f"{name} must be a non-empty revision id")
    return value


def _require_reason(reason: str) -> None:
    if not reason.strip():
        raise ValueError("a transition reason is required")
