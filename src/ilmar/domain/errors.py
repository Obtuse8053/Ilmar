# SPDX-License-Identifier: Apache-2.0
"""Error hierarchy shared by the core and all adapters."""

from __future__ import annotations


class IlmarError(Exception):
    """Base class for all errors raised deliberately by Ilmar."""


class ConfigError(IlmarError):
    """The configuration is missing, malformed or unsafe."""


class InvalidTransitionError(IlmarError):
    """A requested execution state change is not allowed by the state machine."""


class RetryLimitExceededError(InvalidTransitionError):
    """A retry was requested but the execution has used all permitted attempts."""


class ExecutionNotFoundError(IlmarError):
    """No execution exists with the requested identifier."""


class DuplicateExecutionError(IlmarError):
    """The task already has an active (non-terminal) execution, or the id is taken."""


class ConcurrencyConflictError(IlmarError):
    """The stored execution changed since it was read; the write was rejected."""


class ConflictError(IlmarError):
    """An external resource is not in the state the caller expected; nothing was changed."""


class OwnershipConflictError(ConflictError):
    """A resource exists but is not owned by the caller (ADR-0012); it was left untouched."""


class LeaseConflictError(ConflictError):
    """A branch head differs from the expected head; the publish was refused (ADR-0010)."""


class NonFastForwardError(ConflictError):
    """Publishing would rewrite branch history; the publish was refused (ADR-0010)."""


class CandidateRejectedError(IlmarError):
    """Agent output cannot be frozen safely (nested repository, special file, ...)."""


class EvidenceIntegrityError(IlmarError):
    """A stored evidence record does not match its digest: it was altered outside Ilmar."""


class ProviderError(IlmarError):
    """An external provider failed. Adapters wrap provider-specific errors in this."""


class PolicyViolationError(IlmarError):
    """A request exceeds what the deployment permits (for example unrestricted egress)."""


class ContainmentError(ProviderError):
    """A sandbox that had to be removed could not be confirmed gone.

    Its workspace must not be reused, mounted or frozen until removal is
    confirmed. The coordinator records the container for reconciliation
    (ADR-0012, ADR-0014).
    """

    def __init__(self, message: str, *, container_id: str, name: str) -> None:
        super().__init__(message)
        self.container_id = container_id
        self.name = name


class FencingError(ConflictError):
    """This coordinator no longer owns the store or the instance (ADR-0020).

    Raised when a write carries a superseded fence, when no fence is given to
    a store a coordinator has claimed, or when the instance lock is no longer
    held. The holder must perform no further external side effect.
    """


class InstanceLockedError(FencingError):
    """Another coordinator holds this instance's lock; this one must not start."""


class AmbiguousOperationError(IlmarError):
    """An earlier attempt of a side effect may or may not have happened (ADR-0020).

    The operation must be reconciled against the external system before the
    execution can continue. It is never repeated blindly.
    """

    def __init__(self, message: str, *, key: str) -> None:
        super().__init__(message)
        self.key = key


class LedgerIntegrityError(IlmarError):
    """The journal, the ledger or a frozen candidate is inconsistent; work stops (ADR-0020)."""


class ClockBehindError(IlmarError):
    """New work was refused because its time precedes the latest persisted update
    (ADR-0028 §3): the wall clock stepped back. Nothing was written; wait for the
    clock to catch up."""


class BudgetExhaustedError(IlmarError):
    """The store refused to start an attempt: its reservation would exceed an
    allowance (ADR-0029 §2). Nothing was written; the caller suspends."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SuspendedError(IlmarError):
    """The store refused new work: a suspension is in force (ADR-0029 §3).
    Nothing was written."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
