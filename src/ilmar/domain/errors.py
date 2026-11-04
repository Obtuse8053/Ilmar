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


class CandidateRejectedError(IlmarError):
    """Agent output cannot be frozen safely (nested repository, special file, ...)."""


class ProviderError(IlmarError):
    """An external provider failed. Adapters wrap provider-specific errors in this."""
